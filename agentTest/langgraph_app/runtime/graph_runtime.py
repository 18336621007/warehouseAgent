# Graph 运行时依赖构建模块，负责统一初始化共享对象。
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.vectorstores.example_vector_store import ExampleVectorStore
from agentTest.langgraph_app.runtime.graph_logger import get_log_file_path
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec, ToolSecurity
from agentTest.llm import LLM
from agentTest.metadata.mysql_store import load_enriched_columns  # 加载字段类型映射
from agentTest.metadata.mysql_store import init_evaluator_table  # 初始化 Evaluator 评估表
from agentTest.langgraph_app.prompts.sql_prompts import build_sql_generation_prompt
from agentTest.langchain_app.app_builder import build_column_rag
from agentTest.langchain_app.app_builder import build_db_rag
from agentTest.langchain_app.app_builder import build_langchain_tools
from agentTest.langchain_app.app_builder import build_table_rag
from agentTest.langchain_app.app_builder import build_bm25_rag
from agentTest.langgraph_app.services.query_plan_schema_resolver import QueryPlanSchemaResolver
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.metadata.multi_source_meta_provider import MultiSourceMetadataProvider
from agentTest.datasource.registry import DataSourceRegistry
from agentTest.datasource.doris_datasource import DorisDataSource
from agentTest.datasource.trino_datasource import TrinoDataSource
from agentTest.langgraph_app.services.whitelist_filtered_store import WhitelistFilteredVectorStore
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
from agentTest.langgraph_app.skills.skill_loader import build_skill_manager

def build_graph_runtime():
    # 结构化日志由TimedRotatingFileHandler按天滚动，服务启动时保留历史日志

    embedding = BailianEmbeddings()

    # 新增：三层 FAISS 向量库（Advisor 用）
    db_rag = build_db_rag(embedding)
    table_rag = build_table_rag(embedding)
    column_rag = build_column_rag(embedding)

    # 新增：BM25 倒排索引（Advisor 混合检索用）
    bm25_rag = build_bm25_rag()
    bm25_retriever = bm25_rag.get("retriever")

    # Evaluator 示例向量库（高质量对话存储，供 Planner/Advisor/Seeker 检索）
    example_vector_store = ExampleVectorStore(embedding)
    # 初始化 Evaluator MySQL 表（幂等）
    init_evaluator_table()

    # Provider 由 Runtime 统一创建，Tools 和 Resolver 共享缓存
    # 多源元数据：主通道 Hive（表清单/字段结构），Hive 查不到时兜底语义层 physical（如 data_project 天数池表）
    semantic_metadata_provider = SemanticMetadataProvider()
    metadata_provider = MultiSourceMetadataProvider(HiveMetadataProvider(), semantic_metadata_provider)
    tools = build_langchain_tools(
        meta_provider=metadata_provider,
    )
    llm = LLM()

    # 三层向量库加白名单过滤包装（Advisor 检索用，提前为局部变量供工具注册复用）
    db_vector_store = WhitelistFilteredVectorStore(db_rag["vector_store"], metadata_provider, key="database")
    table_vector_store = WhitelistFilteredVectorStore(table_rag["vector_store"], metadata_provider, key="table")
    column_vector_store = WhitelistFilteredVectorStore(column_rag["vector_store"], metadata_provider, key="table")

    # 构建 Advisor 工具（闭包注入向量库/BM25 依赖，替换模块级全局变量）
    advisor_tools = build_advisor_tools(
        db_vector_store,
        table_vector_store,
        column_vector_store,
        bm25_retriever,
    )

    # 统一工具注册表：Seeker 工具组（SQL 执行 + Schema 查询）
    tool_registry = ToolRegistry()
    for t in tools:
        tool_registry.register(ToolSpec(
            name=t.name,
            description=t.description,
            tool=t,
            groups=("seeker",),
        ))
    # 统一工具注册表：Advisor 工具组（分层检索 + 草稿更新 + 落盘结果查询）
    # 检索类与落盘结果工具同时注册进 planner 组，供 M2 Planner ReAct 自主调用
    for t in advisor_tools:
        # 安全元数据集中声明：草稿更新非只读，落盘结果查询限制行数，其余默认只读白名单
        security = ToolSecurity(
            read_only=(t.name != "update_draft_plan"),
            row_limit=100 if t.name == "query_stored_result" else 0,
            whitelist_only=True,
        )
        groups = ("advisor", "planner") if t.name != "update_draft_plan" else ("advisor",)
        tool_registry.register(ToolSpec(
            name=t.name,
            description=t.description,
            tool=t,
            groups=groups,
            security=security,
        ))
    # 统一工具注册表：Planner 专属值探查工具（0 行自愈时用 LIKE 实时确认字段实际取值）
    # 参考 Codex：查不到数据返回用 LIKE 确认具体值，而不是依赖元数据采样猜测
    from agentTest.datasource.hive_datasource import HiveDataSource
    from agentTest.langgraph_app.tools.probe_values_tool import build_probe_values_tool
    # 引擎执行器注册表：查询/值探查按表路由（data_project -> doris，其余 -> trino 优先/hive 兜底）
    datasource_registry = DataSourceRegistry()
    datasource_registry.register("hive", HiveDataSource())
    datasource_registry.register("doris", DorisDataSource())
    datasource_registry.register("trino", TrinoDataSource())
    probe_values_tool = build_probe_values_tool(HiveDataSource(), metadata_provider, engine_registry=datasource_registry)
    tool_registry.register(ToolSpec(
        name="probe_values",
        description=probe_values_tool.description,
        tool=probe_values_tool,
        groups=("planner",),
        security=ToolSecurity(read_only=True, row_limit=0, whitelist_only=True),
    ))
    # 统一工具注册表：Planner 专属语义层检索工具集（LLM 驱动：grep + read + list，优先级高于 RAG）
    # 由 LLM 自己选核心词 grep 全文、按需读指标文件确认口径，程序只保证安全（仅语义层目录）与预算截断
    from agentTest.langgraph_app.tools.semantic_tool import build_semantic_tools
    for semantic_tool in build_semantic_tools():
        tool_registry.register(ToolSpec(
            name=semantic_tool.name,
            description=semantic_tool.description,
            tool=semantic_tool,
            groups=("planner",),
            security=ToolSecurity(read_only=True, row_limit=0, whitelist_only=True),
        ))
    # 统一工具注册表：Planner 专属技能读取工具（渐进式披露，仿 Codex）
    # 每轮只向 LLM 披露所有技能 name+description 索引，选中某技能后由 read_skill 按需读完整正文
    skill_manager = build_skill_manager()
    from agentTest.langgraph_app.tools.skill_tool import build_read_skill_tool
    read_skill_tool = build_read_skill_tool(skill_manager)
    tool_registry.register(ToolSpec(
        name="read_skill",
        description=read_skill_tool.description,
        tool=read_skill_tool,
        groups=("planner",),
        security=ToolSecurity(read_only=True, row_limit=0, whitelist_only=True),
    ))

    # 从 MySQL 加载字段类型映射（度量/维度）与字段枚举值映射，供 generate_sql/Resolver 使用
    import re as _re
    _date_like = _re.compile(r"^\d{6,14}$|^\d{4}-\d{2}-\d{2}$|^\d{4}/\d{1,2}/\d{1,2}$")
    columns = load_enriched_columns()
    field_type_map = {}
    sample_values_map = {}
    sample_values_map_simple = {}
    for col in columns:
        key = f"{col['database_name']}.{col['table_name']}.{col['column_name']}"
        field_type_map[key] = col.get("fields_type", "dimension")
        # 排除日期分区类采样值，只保留业务枚举
        samples = [
            str(v) for v in (col.get("sample_values") or [])
            if str(v).strip() and not _date_like.match(str(v))
        ]
        if samples:
            sample_values_map[key] = samples
            for sample in samples:
                if sample not in sample_values_map_simple.setdefault(col["column_name"], []):
                    sample_values_map_simple[col["column_name"]].append(sample)
    # 同时建一个仅用 column_name 的兜底映射
    field_type_map_simple = {}
    for col in columns:
        field_type_map_simple[col["column_name"]] = col.get("fields_type", "dimension")

    query_plan_schema_resolver = (
        QueryPlanSchemaResolver(
            metadata_provider=metadata_provider,
            sample_values_map=sample_values_map,
        )
    )

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
        "db_vector_store": db_vector_store,
        "table_vector_store": table_vector_store,
        "column_vector_store": column_vector_store,
        "example_vector_store": example_vector_store,  # Evaluator 示例向量库
        # 新增：BM25 倒排索引检索器（混合检索用）
        "bm25_retriever": bm25_retriever,
        "tools": tools,
        "tool_registry": tool_registry,  # 统一工具注册表：节点按组取工具
        "field_type_map": field_type_map,  # 字段类型映射 {db.table.col: measure|dimension}
        "field_type_map_simple": field_type_map_simple,  # 兜底 {col: measure|dimension}
        "sample_values_map": sample_values_map,  # 字段枚举值 {db.table.col: [values]}
        "sample_values_map_simple": sample_values_map_simple,  # 兜底 {col: [values]}
        "semantic_metadata_provider": semantic_metadata_provider,  # join关系
        "datasource_registry": datasource_registry,  # 引擎注册表：按表路由查询/值探查引擎
        # 通用 skill 管理器：Planner 渐进式披露（注入 name+description 索引，正文由 read_skill 按需读取）
        "skill_manager": skill_manager,
    }
