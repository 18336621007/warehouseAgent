# chart_tool.py —— make_chart 工具：程序从落盘结果取数，按统一模板构建图表 spec
# 目标：图表格式 100% 正确。LLM 不再手写 chart JSON，只选择 类型/字段/标题，
#       程序从 execute_query 已落盘的结果里读真实数据、校验并生成规范 ```chart 块，
#       LLM 把返回的代码块原样粘贴进回答，杜绝格式漂移与空白图。
import json
import re

from langchain_core.tools import StructuredTool

from agentTest.langgraph_app.services.result_store import read_result_full
from agentTest.langgraph_app.tools.result_query_tool import get_result_conversation

# 图表最大行数上限（防止超大结果撑爆 prompt/前端）
MAX_CHART_ROWS = 200
# 支持的图表类型（折线/柱状/面积/饼图）
_ALLOWED_TYPES = ("line", "bar", "pie", "area")


def _to_str_list(value) -> list:
    """把 字符串/列表/JSON数组字符串 统一成 list[str]（兼容模型序列化差异）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except Exception:
        pass
    return [text]


def _is_numeric_col(columns, rows, col):
    """判断某列是否数值列（取前 30 行尝试转 float）。"""
    vals = []
    for r in rows[:30]:
        v = r.get(col)
        if v is None or str(v).strip() == "":
            continue
        vals.append(str(v).strip().replace(",", ""))
    if not vals:
        return False
    for v in vals:
        try:
            float(v)
        except (TypeError, ValueError):
            return False
    return True


def _is_date_like(columns, rows, col):
    """判断某列是否日期列：字段名含 日期/date/day/time，或取值形如 2026-08-24。"""
    low = str(col).lower()
    if any(k in low for k in ("date", "day", "time", "pt_dt")):
        return True
    vals = [str(r.get(col) or "").strip() for r in rows[:20] if r.get(col) is not None]
    if vals and all(re.match(r"^\d{4}[-/]?\d{2}[-/]?\d{2}$", v) for v in vals[:10]):
        return True
    return False


def detect_chart_fields(columns: list, rows: list) -> tuple:
    """自动挑选 x 轴（优先日期列，其次首个非数值列）与 y 轴（数值列）。

    供前端『生成图表』按钮与 make_chart 缺省字段时使用，返回 (x_field, y_fields)。
    """
    if not columns or not rows:
        return "", []
    x = ""
    for c in columns:
        if _is_date_like(columns, rows, c):
            x = c
            break
    if not x:
        for c in columns:
            if not _is_numeric_col(columns, rows, c):
                x = c
                break
    if not x:
        # 全部为数值列时以第一个作 x 轴（如按排名）
        x = columns[0]
    ys = [c for c in columns if c != x and _is_numeric_col(columns, rows, c)]
    return x, ys


def build_chart_spec(entry: dict, rows: list, type: str, x_field: str, y_fields: list,
                     title: str = "", x_name: str = "", y_name: str = "", max_rows: int = MAX_CHART_ROWS) -> tuple:
    """从落盘结果条目 + 全量行构建规范图表 spec。

    校验字段、上限行数、数值化 y 字段；返回 (spec, "") 或 (None, 失败原因)，
    make_chart 工具与前端『生成图表』共用同一套逻辑，保证图表格式 100% 一致。
    """
    columns = [str(c) for c in (entry or {}).get("columns") or []]
    col_set = set(columns)
    if x_field and x_field not in col_set:
        return None, f"x_field '{x_field}' 不在结果字段中，可用字段: {', '.join(columns) or '无'}。"
    for f in y_fields:
        if f not in col_set:
            return None, f"y_field '{f}' 不在结果字段中，可用字段: {', '.join(columns) or '无'}。"
    # 未显式指定时自动探测 x/y（前端按钮与 LLM 缺参场景）
    if not x_field or not y_fields:
        auto_x, auto_y = detect_chart_fields(columns, rows)
        if not x_field:
            x_field = auto_x
        if not y_fields:
            y_fields = auto_y
    if not x_field or not y_fields:
        return None, "结果列中未找到适合做图表的字段（需要一个分类/日期列与至少一个数值列）。"
    max_rows = max(1, min(int(max_rows or MAX_CHART_ROWS), MAX_CHART_ROWS))
    chart_rows = []
    for r in rows[:max_rows]:
        item = {x_field: r.get(x_field)}
        for f in y_fields:
            raw = r.get(f)
            try:
                item[f] = float(raw)
            except (TypeError, ValueError):
                item[f] = raw
        chart_rows.append(item)
    if not chart_rows:
        return None, "落盘结果为空，无法生成图表。"
    spec = {
        "type": type,
        "title": title or "",
        "xField": x_field,
        "yField": y_fields if len(y_fields) > 1 else y_fields[0],
        "xName": x_name or x_field,
        "yName": y_name or (y_fields[0] if len(y_fields) == 1 else ""),
        "data": chart_rows,
    }
    return spec, ""


def build_charts_for_request(conversation_id: str, request_id: str, type: str = "line",
                             x_field: str = "", y_fields=None, title: str = "") -> tuple:
    """为某次请求生成图表 spec 列表：匹配该请求下的全部落盘结果（含 _pN 分段），
    每段一个默认图（自动探测字段），供前端『生成图表』按钮使用。

    返回 (specs, "") 或 ([], 原因)；不依赖工具会话上下文，纯读落盘索引。
    """
    from agentTest.langgraph_app.services.result_store import list_result_index, read_result_full
    if not conversation_id or not request_id:
        return [], "缺少会话或请求参数。"
    request_id = str(request_id).strip()
    entries = list_result_index(conversation_id, limit=20)
    matched = [
        e for e in entries
        if str(e.get("source_request_id") or "").startswith(request_id)
        or str(e.get("result_id") or "").startswith(request_id)
    ]
    if not matched:
        return [], "该回答没有可查询的落盘结果，无法生成图表。"
    specs = []
    multiple = len(matched) > 1
    for e in matched:
        data = read_result_full(conversation_id, e.get("result_id"))
        if not data:
            continue
        rows = list(data.get("rows") or [])
        per_title = title
        if multiple:
            per_title = (title + " · " if title else "") + f"第{e.get('round_no')}段"
        spec, err = build_chart_spec(e, rows, type, x_field, y_fields or [], per_title, "", "")
        if spec:
            specs.append(spec)
    if not specs:
        return [], "落盘结果没有适合可视化的数据，无法生成图表。"
    return specs, ""


def _list_result_hints(conversation_id: str) -> str:
    """定位失败时列出最近可用轮次引用，引导 LLM 用正确 result_id/round_no 重试。"""
    try:
        from agentTest.langgraph_app.services.result_store import list_result_index
        entries = list_result_index(conversation_id, limit=5)
        parts = []
        for e in entries:
            parts.append(f"round {e.get('round_no')} -> {e.get('result_id')}（{e.get('row_count')} 行）")
        return "；".join(parts) if parts else ""
    except Exception:
        return ""


def _build_chart_block(spec: dict) -> str:
    """把规范 spec 序列化成 ```chart 代码块（LLM 原样粘贴到回答）。"""
    return "```chart\n" + json.dumps(spec, ensure_ascii=False, indent=2) + "\n```"


def build_make_chart_tool():
    """构建 make_chart 工具：基于 execute_query 落盘结果生成规范图表 spec。"""

    def make_chart(
        result_id: str = "",
        type: str = "line",
        x_field: str = "",
        y_fields: str | list[str] = "",
        title: str = "",
        x_name: str = "",
        y_name: str = "",
        max_rows: int = MAX_CHART_ROWS,
    ) -> str:
        result_id = str(result_id or "").strip()
        x_field = str(x_field or "").strip()
        y_fields = _to_str_list(y_fields)
        if not result_id:
            return "缺少 result_id：请从 execute_query 返回的『结果已落盘』信息中取 result_id。"
        if not x_field:
            return "缺少 x_field：指定图表 x 轴使用的字段名。"
        if not y_fields:
            return "缺少 y_fields：指定图表 y 轴使用的字段名（单值或数组）。"
        if type not in _ALLOWED_TYPES:
            return f"不支持的图表类型 {type}，可选: {', '.join(_ALLOWED_TYPES)}。"
        conversation_id = get_result_conversation()
        if not conversation_id:
            return "当前会话未初始化，无法读取落盘结果。"
        data = read_result_full(conversation_id, result_id)
        if not data:
            # 定位失败时给出可用轮次/result_id，便于 LLM 用正确引用重试
            hints = _list_result_hints(conversation_id)
            return (
                f"无法定位 result_id={result_id} 的落盘结果，请确认 execute_query 返回的 result_id。"
                + (f"当前可用的轮次：{hints}" if hints else "")
            )
        entry = data.get("entry") or {}
        rows = list(data.get("rows") or [])
        spec, err = build_chart_spec(
            entry, rows, type, x_field, y_fields, title, x_name, y_name, max_rows,
        )
        if not spec:
            return err or "无法生成图表。"
        return (
            "图表已生成，请把下面这段 ```chart 代码块【原样粘贴】到回答中展示图表的位置"
            "（不要修改、不要重新生成、不要加任何注释）：\n" + _build_chart_block(spec)
        )

    return StructuredTool.from_function(
        func=make_chart,
        name="make_chart",
        description=(
            "根据 execute_query 已落盘的查询结果生成图表。"
            "参数 result_id 取 execute_query 返回的『结果已落盘』信息中的 result_id；"
            "type 可选 line/bar/pie/area；x_field/y_fields 为结果中的字段名（y_fields 支持数组）；"
            "title/x_name/y_name 为可选的标题与轴含义。"
            "返回一个规范 ```chart 代码块，请把它原样粘贴到最终回答中展示图表，不要手写 chart JSON。"
        ),
    )
