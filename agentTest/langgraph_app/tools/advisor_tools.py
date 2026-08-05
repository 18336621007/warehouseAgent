# Advisor 的分层检索工具与方案提交工具，k 值从 config/advisor.py 读取
from langchain.tools import tool
from agentTest.config.advisor import SEARCH_DB_K, SEARCH_TABLE_K, SEARCH_COLUMN_K
from agentTest.langgraph_app.tools.submit_query_plan import submit_query_plan  # 完整方案提交工具

# 全局变量，由 build_advisor_tools() 注入 FAISS 实例
_db_vector_store = None
_table_vector_store = None
_column_vector_store = None


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
        candidates.append({
            "table": metadata.get("table", ""),
            "field": metadata.get("column", metadata.get("field", "")),
            "semantic_type": metadata.get("fields_type", ""),
            "comment": page_content,
            "aliases": _extract_aliases_from_content(page_content),
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
    docs = _db_vector_store.similarity_search(question, k=SEARCH_DB_K)
    return _format_docs(docs)


@tool
def search_tables(question: str, database: str = "") -> str:
    """搜索与用户问题相关的数据表，指定 database 时只检索该库。"""
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
    candidates = search_column_candidates(question, table=table)
    if not candidates:
        return "未找到匹配结果。"

    lines = []
    for i, candidate in enumerate(candidates, 1):
        lines.append(f"--- 结果 {i} ---")
        lines.append(candidate["comment"][:600])
    return "\n".join(lines)


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
        submit_query_plan,
    ]
