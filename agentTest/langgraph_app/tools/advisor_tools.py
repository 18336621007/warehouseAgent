# Advisor 的分层检索工具与方案提交工具，k 值从 config/advisor.py 读取
from langchain.tools import tool
from agentTest.config.advisor import SEARCH_DB_K, SEARCH_TABLE_K, SEARCH_COLUMN_K, BM25_ALPHA
from agentTest.langchain_app.rag.hybrid_retriever import HybridRetriever

# 全局变量，由 build_advisor_tools() 注入 FAISS 实例和 BM25 实例
_db_vector_store = None
_table_vector_store = None
_column_vector_store = None
_bm25_retriever = None
_hybrid_retriever = None

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
        return []

    # 指定表时：按问题相关度召回该表字段（先精确取字段数保证 fetch_k 覆盖全表，避免截断漏召）
    if table and _column_vector_store is not None:
        table_columns = _column_vector_store.columns_in_table(table)
        if not table_columns:
            return []
        # fetch_k 必须覆盖该表全部字段，否则目标字段可能因相似度排名靠后而被过滤掉
        fetch_k = max(top_k * 5, len(table_columns) * 2, 50)
        docs_with_scores = _column_vector_store.similarity_search_with_score(
            question,
            k=top_k,
            filter={"table": table},
            fetch_k=fetch_k,
        )
        candidates = []
        for doc, score in docs_with_scores:
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
                "score": float(round(float(score), 4)),
            })
        return candidates

    # 使用混合检索
    if _hybrid_retriever:
        docs_with_scores = _hybrid_retriever._search(
            query=question,
            k=top_k,
            vector_store_key="column",
        )
    elif _column_vector_store:
        docs_with_scores = _column_vector_store.similarity_search_with_score(
            question,
            k=top_k,
        )
    else:
        return []

    candidates = []
    for doc, score in docs_with_scores:
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
            "score": float(round(float(score), 4)),
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

    # 使用混合检索
    if _hybrid_retriever:
        docs = _hybrid_retriever.search_databases(question, k=SEARCH_DB_K)
    elif _db_vector_store:
        docs = _db_vector_store.similarity_search(question, k=SEARCH_DB_K)
    else:
        docs = []

    return _format_docs(docs)


@tool
def search_tables(question: str, database: str = "") -> str:
    """搜索与用户问题相关的数据表，指定 database 时只检索该库。"""
    question = str(question or "").strip()
    if not question:
        return "未找到匹配结果。"

    # 使用混合检索
    if _hybrid_retriever:
        docs = _hybrid_retriever.search_tables(question, k=SEARCH_TABLE_K)
    elif _table_vector_store:
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
    else:
        docs = []

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

    草稿是 Planner 判定是否执行的核心依据：你可以只提交部分槽位（如先确认指标、再补维度），
    程序会保留旧方案未修改部分并跨轮保存；不要锁定方案、不要请求用户最终确认，
    是否进入执行由 Planner 统一判定。

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

def build_advisor_tools(
    db_vector_store=None,
    table_vector_store=None,
    column_vector_store=None,
    bm25_retriever=None,
):
    """注入 FAISS 实例和 BM25 实例，构建混合检索器，返回 Advisor 工具列表。

    Args:
        db_vector_store: 数据库层 FAISS 向量库
        table_vector_store: 表层 FAISS 向量库
        column_vector_store: 字段层 FAISS 向量库
        bm25_retriever: BM25 倒排索引检索器
    """
    global _db_vector_store, _table_vector_store, _column_vector_store
    global _bm25_retriever, _hybrid_retriever

    _db_vector_store = db_vector_store
    _table_vector_store = table_vector_store
    _column_vector_store = column_vector_store
    _bm25_retriever = bm25_retriever

    # 构建混合检索器
    if bm25_retriever and (db_vector_store or table_vector_store or column_vector_store):
        vector_stores = {}
        if db_vector_store:
            vector_stores["db"] = db_vector_store
        if table_vector_store:
            vector_stores["table"] = table_vector_store
        if column_vector_store:
            vector_stores["column"] = column_vector_store

        _hybrid_retriever = HybridRetriever(
            bm25_retriever=bm25_retriever,
            vector_stores=vector_stores,
            alpha=BM25_ALPHA,
        )
    else:
        _hybrid_retriever = None

    return [
        search_databases,
        search_tables,
        search_columns,
        update_draft_plan,
    ]
