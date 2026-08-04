# Advisor 的分层检索工具与方案提交工具，k 值从 config/advisor.py 读取
from langchain.tools import tool
from agentTest.config.advisor import SEARCH_DB_K, SEARCH_TABLE_K, SEARCH_COLUMN_K
from agentTest.langgraph_app.tools.submit_query_plan import submit_query_plan  # 完整方案提交工具

# 全局变量，由 build_advisor_tools() 注入 FAISS 实例
_db_vector_store = None
_table_vector_store = None
_column_vector_store = None


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
    if table:
        docs = _column_vector_store.similarity_search(
            question,
            k=SEARCH_COLUMN_K,
            filter={"table": table},
            fetch_k=max(SEARCH_COLUMN_K * 5, 50),
        )
    else:
        docs = _column_vector_store.similarity_search(
            question,
            k=SEARCH_COLUMN_K,
        )

    return _format_docs(docs)


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
