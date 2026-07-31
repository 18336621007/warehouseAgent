# Graph 运行时依赖构建模块，负责统一初始化共享对象。
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.vectorstores.example_vector_store import ExampleVectorStore
from agentTest.langgraph_app.runtime.graph_logger import clear_log_file
from agentTest.langgraph_app.runtime.graph_logger import get_log_file_path
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.llm import LLM
from agentTest.metadata.mysql_store import load_enriched_columns  # 加载字段类型映射
from agentTest.metadata.mysql_store import init_evaluator_table  # 初始化 Evaluator 评估表
from agentTest.langgraph_app.prompts.sql_generation_prompt import build_sql_generation_prompt  # 新增
from agentTest.langchain_app.app_builder import build_column_rag
from agentTest.langchain_app.app_builder import build_db_rag
from agentTest.langchain_app.app_builder import build_langchain_tools
from agentTest.langchain_app.app_builder import build_table_rag
from agentTest.langgraph_app.services.query_plan_schema_resolver import QueryPlanSchemaResolver
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider

def build_graph_runtime():

    # 清空旧日志，保证每次运行都从新日志开始
    clear_log_file()

    embedding = BailianEmbeddings()

    # 新增：三层 FAISS 向量库（Advisor 用）
    db_rag = build_db_rag(embedding)
    table_rag = build_table_rag(embedding)
    column_rag = build_column_rag(embedding)



    # Evaluator 示例向量库（高质量对话存储，供 Planner/Advisor/Seeker 检索）
    example_vector_store = ExampleVectorStore(embedding)
    # 初始化 Evaluator MySQL 表（幂等）
    init_evaluator_table()

    # Provider 由 Runtime 统一创建，Tools 和 Resolver 共享缓存
    metadata_provider = HiveMetadataProvider()
    tools = build_langchain_tools(
        meta_provider=metadata_provider,
    )
    query_plan_schema_resolver = (
        QueryPlanSchemaResolver(
            metadata_provider=metadata_provider,
        )
    )

    llm = LLM()

    # 从 MySQL 加载字段类型映射（度量/维度），供 generate_sql 生成聚合 SQL
    columns = load_enriched_columns()
    field_type_map = {}
    for col in columns:
        key = f"{col['database_name']}.{col['table_name']}.{col['column_name']}"
        field_type_map[key] = col.get("fields_type", "dimension")
    # 同时建一个仅用 column_name 的兜底映射
    field_type_map_simple = {}
    for col in columns:
        field_type_map_simple[col["column_name"]] = col.get("fields_type", "dimension")

    # 记录 runtime 初始化完成日志
    log_node_event("runtime", f"初始化完成, 日志: {get_log_file_path()}")

    return {
        "embedding": embedding,
        "llm": llm,
        "prompt": build_sql_generation_prompt(),  # prompt 直接构建，不依赖 Hive
        # Seeker 使用确认方案精确加载 Schema
        "query_plan_schema_resolver": (
            query_plan_schema_resolver
        ),
        # 新增：三层向量库（Advisor 用）
        "db_vector_store": db_rag["vector_store"],
        "table_vector_store": table_rag["vector_store"],
        "column_vector_store": column_rag["vector_store"],
        "example_vector_store": example_vector_store,  # Evaluator 示例向量库
        "tools": tools,
        "field_type_map": field_type_map,  # 字段类型映射 {db.table.col: measure|dimension}
        "field_type_map_simple": field_type_map_simple,  # 兜底 {col: measure|dimension}
    }

