# result_query_tool.py —— 受控只读中间结果查询工具
# 职责：基于 result_store 落盘的 CSV 做确定性计算（查看/分组计数/分组求和/筛选计数），
#       字段白名单 + 行数上限，只读本会话落盘目录，不执行任意 SQL/Python。
import re
from contextvars import ContextVar

from langchain_core.tools import StructuredTool
from agentTest.langgraph_app.services.result_store import read_result_full

# 单次返回行数上限（防止把全量结果塞进 prompt）
MAX_RESULT_QUERY_LIMIT = 100
# 分组字段个数上限，防止滥用
MAX_GROUP_BY_FIELDS = 3

# 当前会话上下文：由调用方（如 Advisor run_advisor）在每轮入口设置，
# 工具内部据此只读本会话落盘目录，避免跨会话读取
_current_conversation_id = ContextVar("query_stored_result_conversation_id", default="")


def set_result_conversation(conversation_id: str):
    """设置当前会话 id，返回 contextvar token（调用方需 finally reset）。"""
    return _current_conversation_id.set(str(conversation_id or ""))


def reset_result_conversation(token):
    """复位会话上下文，防止跨请求串会话。"""
    _current_conversation_id.reset(token)


_FILTER_SEG_RE = re.compile(
    r"^\s*`?([A-Za-z_]\w*)`?\s*(=|!=|<>|>=|<=|>|<)\s*"
    r"(?:'([^']*)'|\"([^\"]*)\"|(-?\d+(?:\.\d+)?))\s*$"
)


def _parse_filters(filters: str) -> list[tuple[str, str, str]]:
    """解析简单过滤条件："field='value' AND field2>=x"，返回 [(字段, 比较符, 值)]。"""
    if not filters or not str(filters).strip():
        return []
    segments = re.split(r"\s+AND\s+", str(filters).strip(), flags=re.IGNORECASE)
    parsed = []
    for seg in segments:
        m = _FILTER_SEG_RE.match(seg)
        if not m:
            raise ValueError(f"无法解析过滤条件: {seg}")
        field, op = m.group(1), m.group(2)
        value = m.group(3) if m.group(3) is not None else (m.group(4) if m.group(4) is not None else m.group(5))
        parsed.append((field, op, value))
    return parsed


def _row_matches(row: dict, parsed: list[tuple[str, str, str]]) -> bool:
    """判断一行是否满足全部过滤条件（字符串/数值比较）。"""
    for field, op, value in parsed:
        cell = row.get(field)
        if cell is None:
            return False
        try:
            left = float(str(cell))
            right = float(value)
            numeric = True
        except (TypeError, ValueError):
            left = str(cell)
            right = value
            numeric = False
        if op in ("=", "=="):
            if not (left == right):
                return False
        elif op in ("!=", "<>"):
            if not (left != right):
                return False
        elif op == ">=":
            if not (left >= right):
                return False
        elif op == "<=":
            if not (left <= right):
                return False
        elif op == ">":
            if not (left > right):
                return False
        elif op == "<":
            if not (left < right):
                return False
    return True


def _render_view(rows: list, columns: list, limit: int) -> str:
    """把行渲染成 markdown 表格，只展示前 limit 行。"""
    if not rows:
        return "（无数据）"
    lines = []
    for row in rows[:limit]:
        cells = [str(row.get(c, "")) for c in columns]
        lines.append("| " + " | ".join(cells) + " |")
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    text = header + "\n" + sep + "\n" + "\n".join(lines)
    if len(rows) > limit:
        text += f"\n……（共 {len(rows)} 行，仅展示前 {limit} 行，全量见 CSV 文件）"
    return text


def _render_group_count(rows: list, group_by: list) -> str:
    """按分组字段统计行数（降序）。"""
    from collections import Counter
    counter = Counter()
    for row in rows:
        key = tuple(str(row.get(f, "")) for f in group_by)
        counter[key] += 1
    if not counter:
        return "（无数据）"
    label = " / ".join(group_by) if group_by else "全部"
    lines = [f"按 {label} 分组计数："]
    for key, count in counter.most_common():
        key_text = " / ".join(key) if len(key) > 1 else key[0]
        lines.append(f"- {key_text or '(空)'}: {count}")
    return "\n".join(lines)


def _render_group_sum(rows: list, group_by: list, agg_field: str) -> str:
    """按分组字段对 agg_field 求和（降序），非数值跳过。"""
    from collections import defaultdict
    sums = defaultdict(float)
    for row in rows:
        key = tuple(str(row.get(f, "")) for f in group_by)
        try:
            sums[key] += float(row.get(agg_field, 0) or 0)
        except (TypeError, ValueError):
            continue
    if not sums:
        return "（无数据）"
    label = " / ".join(group_by) if group_by else "全部"
    lines = [f"按 {label} 分组对 {agg_field} 求和："]
    for key, total in sorted(sums.items(), key=lambda kv: -kv[1]):
        key_text = " / ".join(key) if len(key) > 1 else key[0]
        lines.append(f"- {key_text or '(空)'}: {total:g}")
    return "\n".join(lines)


def build_result_query_tool(default_ref: str = ""):
    """构建受控的落盘结果查询工具；会话 id 由 set_result_conversation 注入。

    ref 未传时回退 default_ref（调用方已定位到的轮次）。
    """
    def query_stored_result(
        ref: str = "",
        operation: str = "view",
        group_by: list[str] | None = None,
        agg_field: str = "",
        filters: str = "",
        limit: int = 20,
    ) -> str:
        ref = str(ref or default_ref or "").strip()
        if not ref:
            return "缺少 ref 参数：请指定要读取的轮次（round_no 或 result_id）。"
        conversation_id = _current_conversation_id.get()
        if not conversation_id:
            return "当前会话未初始化，无法读取落盘结果。"
        operation = str(operation or "view").strip().lower()
        if operation not in ("view", "group_by_count", "group_by_sum", "filter_count"):
            return (
                f"不支持的 operation: {operation}，可选: "
                "view/group_by_count/group_by_sum/filter_count"
            )
        data = read_result_full(conversation_id, ref)
        if not data:
            return f"无法定位第 {ref} 轮结果，请确认轮次是否正确。"
        entry = data["entry"]
        rows = data["rows"]
        columns = list(entry.get("columns") or [])
        round_no = entry.get("round_no", ref)
        full_csv = str(entry.get("full_csv") or "")
        col_set = set(columns)
        group_by = [str(f) for f in (group_by or []) if str(f).strip()]
        if len(group_by) > MAX_GROUP_BY_FIELDS:
            return f"分组字段过多（最多 {MAX_GROUP_BY_FIELDS} 个）。"
        for f in group_by:
            if f not in col_set:
                return f"字段 {f} 不在第 {round_no} 轮结果列中，可用列: {', '.join(columns)}"
        if agg_field and agg_field not in col_set:
            return f"字段 {agg_field} 不在第 {round_no} 轮结果列中，可用列: {', '.join(columns)}"
        try:
            parsed = _parse_filters(filters)
        except ValueError as err:
            return f"过滤条件解析失败：{err}"
        for field, _, _ in parsed:
            if field not in col_set:
                return f"过滤字段 {field} 不在第 {round_no} 轮结果列中，可用列: {', '.join(columns)}"
        limit = max(1, min(int(limit or 20), MAX_RESULT_QUERY_LIMIT))
        header = f"第{round_no}轮结果（共 {len(rows)} 行，列: {', '.join(columns)}）"
        if operation == "view":
            body = _render_view(rows, columns, limit)
        elif operation == "group_by_count":
            body = _render_group_count(rows, group_by)
        elif operation == "group_by_sum":
            body = _render_group_sum(rows, group_by, agg_field)
        else:  # filter_count
            matched = [r for r in rows if _row_matches(r, parsed)]
            body = f"筛选后共 {len(matched)} 行。\n" + _render_view(matched, columns, limit)
        if full_csv:
            body += f"\n全量数据文件：{full_csv}"
        return header + "\n" + body

    return StructuredTool.from_function(
        func=query_stored_result,
        name="query_stored_result",
        description=(
            "读取本对话某轮已落盘的查询结果（只读、受控，不查询数据库）。"
            "ref 传轮次 round_no 或 result_id；operation 可选 view/group_by_count/group_by_sum/filter_count；"
            "group_by 为分组字段列表，agg_field 为求和字段，filters 为过滤条件（如 disable_type='批量召回'），"
            "所有字段必须是该轮结果中的列。"
        ),
    )
