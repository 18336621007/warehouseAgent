# Advisor 的分层检索工具与方案提交工具，k 值从 config/advisor.py 读取
from langchain.tools import tool
from agentTest.config.advisor import SEARCH_DB_K, SEARCH_TABLE_K, SEARCH_COLUMN_K
from agentTest.langgraph_app.tools.submit_query_plan import submit_query_plan  # 完整方案提交工具

# 全局变量，由 build_advisor_tools() 注入 FAISS 实例
_db_vector_store = None
_table_vector_store = None
_column_vector_store = None

# 增强元数据枚举缓存：{table_name: {column_name: [枚举值]}} 与 {column_name: [跨表枚举参考]}
_ENRICHED_ENUM_INDEX = None
_ENRICHED_ENUM_INDEX_SIMPLE = None
# 日期分区类值（yyyyMMdd / yyyyMM / yyyy-MM-dd 等）不属于业务枚举，过滤避免误导
_DATE_LIKE_PATTERN = None


def _is_enum_candidate_value(value: str) -> bool:
    """判断采样值是否可能是业务枚举值（排除日期分区/纯数字流水）。"""
    global _DATE_LIKE_PATTERN
    if _DATE_LIKE_PATTERN is None:
        import re
        _DATE_LIKE_PATTERN = re.compile(
            r"^\d{6,14}$|^\d{4}-\d{2}-\d{2}$|^\d{4}/\d{1,2}/\d{1,2}$"
        )
    if not value or _DATE_LIKE_PATTERN.match(value):
        return False
    return True


def _ensure_enum_index():
    """惰性加载 enriched_columns 的枚举值索引（运行时只加载一次）。"""
    global _ENRICHED_ENUM_INDEX, _ENRICHED_ENUM_INDEX_SIMPLE
    if _ENRICHED_ENUM_INDEX is not None:
        return
    from agentTest.metadata.mysql_store import load_enriched_columns
    index = {}
    index_simple = {}
    for col in load_enriched_columns():
        table_name = col.get("table_name", "")
        column_name = col.get("column_name", "")
        samples = [
            str(v) for v in (col.get("sample_values") or [])
            if str(v).strip() and _is_enum_candidate_value(str(v))
        ]
        if table_name and column_name and samples:
            index.setdefault(table_name, {})[column_name] = samples
        if column_name:
            for sample in samples:
                if sample not in index_simple.setdefault(column_name, []):
                    index_simple[column_name].append(sample)
    _ENRICHED_ENUM_INDEX = index
    _ENRICHED_ENUM_INDEX_SIMPLE = index_simple


def _build_enum_hint(field: str, table: str = "") -> str:
    """构造字段枚举提示：优先本表采样值，缺失时回退同名列其他表作为参考。"""
    _ensure_enum_index()
    field = str(field or "")
    table_name = table.split(".")[-1] if "." in table else table
    samples = (_ENRICHED_ENUM_INDEX or {}).get(table_name, {}).get(field, [])
    if samples:
        return "枚举值: " + "、".join(samples)
    ref = (_ENRICHED_ENUM_INDEX_SIMPLE or {}).get(field, [])
    # 参考值过多说明不是稳定业务枚举，避免刷屏误导
    if ref and len(ref) <= 20:
        return "枚举参考（来自其他表，需以本表实际数据为准）: " + "、".join(ref)
    return ""


def _extract_aliases_from_content(page_content: str) -> list[str]:
    """从字段检索文本中解析“别名: xxx、yyy”。"""
    import re
    match = re.search(r"别名:\s*(.+)", page_content or "")
    if not match:
        return []
    return [
        alias.strip()
        for alias in match.group(1).split("、")
        if alias.strip()
    ]


def search_column_candidates(question: str, table: str = "", k: int = None) -> list[dict]:
    """返回结构化字段候选，供指标歧义门禁程序化校验，不直接格式化给 LLM。"""
    top_k = k or SEARCH_COLUMN_K
    question = str(question or "").strip()
    if not question:
        # 空查询无法生成向量，直接返回空候选，避免 embedding 接口 400
        return []
    if table:
        docs_with_scores = _column_vector_store.similarity_search_with_score(
            question,
            k=top_k,
            filter={"table": table},
            fetch_k=max(top_k * 5, 50),
        )
    else:
        docs_with_scores = _column_vector_store.similarity_search_with_score(
            question,
            k=top_k,
        )

    candidates = []
    for doc, distance in docs_with_scores:
        metadata = doc.metadata or {}
        page_content = doc.page_content or ""
        field = metadata.get("column", metadata.get("field", ""))
        candidates.append({
            "table": metadata.get("table", ""),
            "field": field,
            "semantic_type": metadata.get("fields_type", ""),
            "comment": page_content,
            "aliases": _extract_aliases_from_content(page_content),
            "enum_hint": _build_enum_hint(field, metadata.get("table", "")),
            "score": float(round(1 - float(distance) / 2, 4)),
        })
    return candidates


def _format_docs(docs) -> str:
    """把 Document 列表格式化为 LLM 可读文本"""
    if not docs:
        return "未找到匹配结果。"
    lines = []
    for i, doc in enumerate(docs):
        lines.append(f"--- 结果 {i+1} ---")
        lines.append(doc.page_content[:600])
    return "\n".join(lines)


@tool
def search_databases(question: str) -> str:
    """搜索与用户问题相关的数据库。返回库名、领域、描述。"""
    question = str(question or "").strip()
    if not question:
        return "未找到匹配结果。"
    docs = _db_vector_store.similarity_search(question, k=SEARCH_DB_K)
    return _format_docs(docs)


@tool
def search_tables(question: str, database: str = "") -> str:
    """搜索与用户问题相关的数据表，指定 database 时只检索该库。"""
    question = str(question or "").strip()
    if not question:
        return "未找到匹配结果。"
    if database:
        docs = _table_vector_store.similarity_search(
            question,
            k=SEARCH_TABLE_K,
            filter={"database": database},
            fetch_k=max(SEARCH_TABLE_K * 5, 50),
        )
    else:
        docs = _table_vector_store.similarity_search(
            question,
            k=SEARCH_TABLE_K,
        )

    return _format_docs(docs)


@tool
def search_columns(question: str, table: str = "") -> str:
    """搜索与用户问题相关的字段，指定 table 时只检索该表字段。"""
    question = str(question or "").strip()
    if not question:
        return "未找到匹配结果。"
    candidates = search_column_candidates(question, table=table)
    if not candidates:
        return "未找到匹配结果。"

    lines = []
    for i, candidate in enumerate(candidates, 1):
        lines.append(f"--- 结果 {i} ---")
        comment = candidate["comment"][:600]
        lines.append(comment)
        # 字段无采样值时附加枚举提示，让模型从枚举值中选择而不是猜测
        enum_hint = candidate.get("enum_hint", "")
        if enum_hint and "采样值:" not in comment:
            lines.append(enum_hint)
    return "\n".join(lines)



@tool
def update_draft_plan(
    tables: list[str] = None,
    measures: list[str] = None,
    dimensions: list[str] = None,
    time_field: str = "",
    time_range: str = "",
    filters: str = "",
    field_sources: list[str] = None,   # ["db.table.field", ...]
    order_by: list[dict] = None,       # [{"field": "new_order", "direction": "DESC"}]
    having: str = "",
    result_limit: int = 1000,
    complex: bool = False,
    concept_resolutions: list[dict] = None,  # 指标解析证据（审计用）
) -> str:
    """在追问过程中，把当前已确认的查询方案部分写入草稿状态（status=draft）。

    与 submit_query_plan 的区别：本工具允许只提交部分槽位（如先确认指标、再补维度），
    程序会保留旧方案未修改部分；当你判断查询方案已满足查询需求时，
    必须调用 submit_query_plan 提交完整方案并等待用户最终确认。

    参数说明：
    - tables: 查询涉及的全部表列表，单表如 ["ads_trip.xxx"]，多表如 ["ads_trip.xxx", "dim_trip.yyy"]
    - measures: 度量字段列表（裸字段名），无则传 []
    - dimensions: 维度字段列表（裸字段名），无则传 []
    - time_field: 目标表中的时间字段
    - time_range: 用户确认的时间范围，如 昨天、最近7天
    - filters: 额外过滤条件，没有时传 ""
    - field_sources: 每个字段的完整物理标识列表，格式 ["db.table.field", ...]
    - concept_resolutions: 指标解析证据列表，字段必须来自候选列表或上轮已确认字段
    """
    return (
        f"方案草稿已更新: 表={tables}, 度量={measures}, "
        f"维度={dimensions}, 时间={time_field}({time_range or '未指定'}), "
        f"过滤={filters or '无'}"
    )

def build_advisor_tools(db_vector_store, table_vector_store, column_vector_store):
    """注入三层 FAISS 实例，返回 Advisor 工具列表。"""
    global _db_vector_store, _table_vector_store, _column_vector_store
    _db_vector_store = db_vector_store
    _table_vector_store = table_vector_store
    _column_vector_store = column_vector_store

    return [
        search_databases,
        search_tables,
        search_columns,
        update_draft_plan,
        submit_query_plan,
    ]
