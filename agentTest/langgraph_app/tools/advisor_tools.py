# Advisor 的分层检索工具与方案提交工具，k 值从 config/advisor.py 读取
from langchain.tools import tool
from agentTest.config.advisor import SEARCH_DB_K, SEARCH_TABLE_K, SEARCH_COLUMN_K, BM25_ALPHA
from agentTest.langchain_app.rag.hybrid_retriever import HybridRetriever
from agentTest.langgraph_app.tools.result_query_tool import build_result_query_tool

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
    select_fields: list[str] = None,
    filters: str = "",
    detail_query: bool = False,
) -> str:
    """在追问过程中，把当前已确认的查询方案部分写入草稿状态（status=draft）。

    草稿只保存两类业务方案字段：查看字段（select_fields）+ 过滤字段（filters，含时间），
    聚合/分组/时间字段由程序统一派生，不需要手工区分。可以只提交部分字段，
    程序会保留旧方案未修改部分并跨轮保存；不要锁定方案，是否执行由 Planner 判定。

    参数说明：
    - tables: 查询涉及的全部表列表，单表如 ["ads_trip.xxx"]，多表如 ["ads_trip.xxx", "dim_trip.yyy"]
    - select_fields: 需要查看的字段列表（度量+维度+明细字段统一收口），
      可为裸字段名或完整路径 "db.table.field"；无则传 []
    - filters: 过滤条件（含时间），多个条件用 AND 连接，如
      "create_time >= '2026-01-01' AND create_time <= '2026-12-31' AND region_name='徐州大区' AND status='同意返厂'"；没有时传 ""
    - detail_query: 是否为明细型查询（不聚合、不 GROUP BY），默认 False
    """
    return (
        f"方案草稿已更新: 表={tables}, 查看字段={select_fields}, "
        f"过滤={filters or '无'}, 明细查询={detail_query}"
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
        # 受控读落盘中间结果：会话 id 由 run_advisor 每轮注入 contextvar
        build_result_query_tool(),
    ]
