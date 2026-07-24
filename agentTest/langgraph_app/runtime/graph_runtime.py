# Graph 运行时依赖构建模块，负责统一初始化共享对象。
from agentTest.langchain_app.app_builder import build_schema_rag_app, build_db_rag, build_table_rag, build_column_rag
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langgraph_app.runtime.graph_logger import clear_log_file
from agentTest.langgraph_app.runtime.graph_logger import get_log_file_path
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.llm import LLM
from agentTest.langchain_app.app_builder import build_enriched_schema_rag_app, build_langchain_tools
from agentTest.langgraph_app.prompts.sql_generation_prompt import build_sql_generation_prompt  # 新增


def build_graph_runtime():

    # 清空旧日志，保证每次运行都从新日志开始
    clear_log_file()

    embedding = BailianEmbeddings()

    # 新增：三层 FAISS 向量库（Advisor 用）
    db_rag = build_db_rag(embedding)
    table_rag = build_table_rag(embedding)
    column_rag = build_column_rag(embedding)

    # 保留：旧的单层版本（Seeker 暂时还用）
    enriched_context = build_enriched_schema_rag_app(embedding) # 论文增强后的schema

    tools = build_langchain_tools()
    llm = LLM()

    # 记录 runtime 初始化完成日志
    log_node_event("graph_runtime", f"runtime initialized, log_file={get_log_file_path()}")

    return {
        "embedding": embedding,
        "llm": llm,
        "prompt": build_sql_generation_prompt(),  # prompt 直接构建，不依赖 Hive
        "retriever": enriched_context["retriever"],  # 增强版检索器
        # 新增：三层向量库（Advisor 用）
        "db_vector_store": db_rag["vector_store"],
        "table_vector_store": table_rag["vector_store"],
        "column_vector_store": column_rag["vector_store"],
        "tools": tools
    }
"""
为什么 prompt 可以直接构建？
build_sql_generation_prompt() 只是创建一个 ChatPromptTemplate 对象，纯 Python 操作，不需要数据库。

为什么不继续用 build_schema_rag_app？
因为它的 build_documents() 每次都会连 Hive 做 list_tables + describe_table，
即使 FAISS 缓存了磁盘也不会跳过这一步。换成增强版后，整个 graph_runtime 启动就只依赖 MySQL + FAISS 磁盘缓存，不再需要 Hive 连接。
"""