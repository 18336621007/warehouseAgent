# chart_tool.py —— make_chart 工具：程序从落盘结果取数，按统一模板构建图表 spec
# 目标：图表格式 100% 正确。LLM 不再手写 chart JSON，只选择 类型/字段/标题，
#       程序从 execute_query 已落盘的结果里读真实数据、校验并生成规范 ```chart 块，
#       LLM 把返回的代码块原样粘贴进回答，杜绝格式漂移与空白图。
import json
import re

from langchain_core.tools import StructuredTool

from agentTest.langgraph_app.services.result_store import read_result_full
from agentTest.langgraph_app.tools.result_query_tool import get_result_conversation


def _extract_chart_meta(answer_text: str) -> dict:
    """从回答文本的 ```chart 块中复用模型已提供的中文元数据（字段/轴名/系列名/标题）。"""
    m = re.search(r"```\s*chart\s*\n(.*?)```", answer_text or "", re.S)
    if not m:
        return {}
    try:
        spec = json.loads(m.group(1))
    except (Exception, json.JSONDecodeError):
        return {}
    yf = spec.get("yFields") or spec.get("yField") or []
    if not isinstance(yf, list):
        yf = [yf] if yf else []
    return {
        "x_field": spec.get("xField") or "",
        "y_fields": [str(v) for v in yf],
        "x_name": spec.get("xName") or "",
        "y_name": spec.get("yName") or "",
        "series_names": [str(v) for v in (spec.get("seriesNames") or [])],
        "title": spec.get("title") or "",
    }


# 图表最大行数上限（防止超大结果撑爆 prompt/前端）
MAX_CHART_ROWS = 200
# 支持的图表类型（折线/柱状/面积/饼图/词云）
_ALLOWED_TYPES = ("line", "bar", "pie", "area", "wordcloud")


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


def _auto_chart_type(columns: list, rows: list, effective_query: str = "") -> str:
    """按数据形态与查询意图自动挑选图表类型（前端『生成图表』按钮用）。

    时间序列 → line（趋势）；问题含占比/构成 → pie；对比/排行 → bar；
    其余：类别数少用 bar，类别多/连续用 line。返回 _ALLOWED_TYPES 之一。
    """
    x, _ = detect_chart_fields(columns, rows)
    if x and _is_date_like(columns, rows, x):
        return "line"
    q = str(effective_query or "")
    if any(k in q for k in ("占比", "比例", "份额", "构成", "分布", "结构")):
        return "pie"
    # 非时间序列（分类维度）默认柱状图即可（对比/排行/多类别都适合）
    return "bar"


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
                     title: str = "", x_name: str = "", y_name: str = "",
                     series_names: list | None = None, max_rows: int = MAX_CHART_ROWS,
                     pie_slices: int = 15) -> tuple:
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
    if type == "wordcloud":
        # 关键词词云：y_fields[0]=关键词列，y_fields[1]=数值列（字号大小与数值正相关）
        if len(y_fields) < 2:
            return None, "wordcloud 需要 y_fields 传两个字段：[关键词列, 数值列]。"
        word_field, value_field = y_fields[0], y_fields[1]
        max_rows = max(1, min(int(max_rows or MAX_CHART_ROWS), MAX_CHART_ROWS))
        chart_rows = []
        for r in rows[:max_rows]:
            raw = r.get(value_field)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = raw
            chart_rows.append({word_field: r.get(word_field), value_field: val})
        if not chart_rows:
            return None, "落盘结果为空，无法生成词云。"
        spec = {
            "type": "wordcloud",
            "title": title or "",
            "wordField": word_field,
            "valueField": value_field,
            "yName": y_name,
            "data": chart_rows,
        }
        return spec, ""
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
    MAX_PIE_SLICES = max(1, min(int(pie_slices or 15), 300))
    spec = {
        "type": type,
        "title": title or "",
        "xField": x_field,
        "yField": y_fields if len(y_fields) > 1 else y_fields[0],
        "xName": x_name,
        "yName": y_name,
        "seriesNames": [str(sn) for sn in (series_names or [])],
        "data": chart_rows,
        "pieMaxSlices": MAX_PIE_SLICES,
    }
    return spec, ""


def _pick_primary_result(conversation_id: str, request_id: str, answer: str = ""):
    """从某请求的全部落盘结果里选出一个主结果（供手动生图）。

    『生成图表』按钮应该只画该回答真正展示的那一个结果；优先匹配回答中引用的落盘文件，
    否则取可成图且行数最大的结果，避免把分区探查/探字段这类过程性小结果混进来。
    """
    from agentTest.langgraph_app.services.result_store import list_result_index
    if not conversation_id or not request_id:
        return None
    request_id = str(request_id).strip()
    entries = list_result_index(conversation_id, limit=20)
    matched = [
        e for e in entries
        if str(e.get("source_request_id") or "").startswith(request_id)
        or str(e.get("result_id") or "").startswith(request_id)
    ]
    if not matched:
        return None
    # 回答里显式引用了某个落盘文件时优先返回它（即“真正展示的那一个”）
    answer_text = str(answer or "")
    for e in matched:
        csv_name = str(e.get("full_csv") or "")
        csv_path = str(e.get("full_csv_path") or "")
        if (csv_name and csv_name in answer_text) or (csv_path and csv_path in answer_text):
            return e
    # 否则挑可成图且行数最大的结果（探查类小结果行数少，自然排后）
    best = None
    for e in matched:
        rows = list(e.get("preview_rows") or [])
        x, ys = detect_chart_fields(list(e.get("columns") or []), rows)
        if not x or not ys:
            continue
        if best is None or (int(e.get("row_count") or 0) > int(best.get("row_count") or 0)):
            best = e
    return best


def build_charts_for_request(conversation_id: str, request_id: str, type: str = "line",
                             x_field: str = "", y_fields=None, title: str = "",
                             answer: str = "") -> tuple:
    """为某次请求生成图表 spec：只返回该回答的一个主结果图表，供前端『生成图表』按钮使用。

    返回 (specs, "") 或 ([], 原因)；不依赖工具会话上下文，纯读落盘索引。
    """
    from agentTest.langgraph_app.services.result_store import read_result_full
    if not conversation_id or not request_id:
        return [], "缺少会话或请求参数。"
    entry = _pick_primary_result(conversation_id, request_id, answer)
    if entry is None:
        return [], "该回答没有适合可视化的落盘结果，无法生成图表。"
    data = read_result_full(conversation_id, entry.get("result_id"))
    if not data:
        return [], "主结果读取失败，无法生成图表。"
    rows = list(data.get("rows") or [])
    # type 缺省或 "auto" 时按数据形态/查询意图自动挑选（占比→pie、趋势→line、对比→bar）
    per_type = type if type and type != "auto" else _auto_chart_type(
        [str(c) for c in (entry.get("columns") or [])], rows, entry.get("effective_query") or "",
    )
    per_title = title or str(entry.get("effective_query") or "") or f"第{entry.get('round_no')}段"
    # 手动『生成图表』优先复用回答里 make_chart 已生成的中文元数据，避免丢失指标名导致图表无法区分
    _meta = _extract_chart_meta(answer)
    _xf = x_field or str(_meta.get("x_field") or "")
    _yfs = y_fields or list(_meta.get("y_fields") or [])
    _xn = str(_meta.get("x_name") or "")
    _yn = str(_meta.get("y_name") or "")
    _sns = list(_meta.get("series_names") or [])
    if not title and _meta.get("title"):
        per_title = _meta["title"]
    spec, err = build_chart_spec(entry, rows, per_type, _xf, _yfs,
                                per_title, _xn, _yn, _sns, MAX_CHART_ROWS, 15)
    if not spec:
        return [], err or "无法生成图表。"
    return [spec], ""



def has_chartable_result(conversation_id: str, request_id: str) -> bool:
    """判断该请求是否真的能生成图表（供前端『生成图表』按钮显隐）。

    先用落盘索引的预览行做轻量探测，明显不可成图直接返回 False，避免读全量 CSV；
    预览可成图时再用 build_charts_for_request 同一套逻辑核验成功，保证按钮可见
    与点击后能成功生成完全一致，不会出现点完才报无法生成。
    """
    from agentTest.langgraph_app.services.result_store import list_result_index
    if not conversation_id or not request_id:
        return False
    request_id = str(request_id or "").strip()
    try:
        entries = list_result_index(conversation_id, limit=20)
        matched = [
            e for e in entries
            if str(e.get("source_request_id") or "").startswith(request_id)
            or str(e.get("result_id") or "").startswith(request_id)
        ]
        if not matched:
            return False
        for e in matched:
            columns = list(e.get("columns") or [])
            rows = list(e.get("preview_rows") or [])
            if not columns or not rows:
                continue
            x, ys = detect_chart_fields(columns, rows)
            if x and ys:
                break
        else:
            return False
        # 预览可成图：再用真实全量构建核验，保证与点击后返回的图表一致
        specs, _err = build_charts_for_request(conversation_id, request_id, type="auto")
        return bool(specs)
    except Exception:
        # 图表探测失败不应影响回答落盘：按钮不显示即可
        return False


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
        series_names: list = [],
        max_rows: int = MAX_CHART_ROWS,
        pie_slices: int = 15,
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
            return "不支持的图表类型 {}，可选: {}。".format(type, "、".join(_ALLOWED_TYPES))
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
            entry, rows, type, x_field, y_fields, title, x_name, y_name,
            series_names, max_rows, pie_slices,
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
            "type 可选 line/bar/pie/area/wordcloud；wordcloud 时 y_fields=[关键词列,数值列]；x_field/y_fields 为结果中的字段名（y_fields 支持数组）；"
            "饼图/环形图默认展示分区数量由 pie_slices 指定（可选，默认 15，最大 300），该值会写入备注说明；"
            "title/x_name/y_name/series_names 用中文业务含义填写（如 新增订单数、平台），避免图表出现英文字段名；series_names 为 y 轴各系列的中文名，数量应与 y_fields 一致。"
            "返回一个规范 ```chart 代码块，请把它原样粘贴到最终回答中展示图表，不要手写 chart JSON。"
        ),
    )
