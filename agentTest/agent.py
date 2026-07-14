from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.datasource.mysql_datasource import MySQLDataSource
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.metadata.mysql_metadata_provider import MySQLMetadataProvider
from agentTest.rag.schema_document_builder import SchemaDocumentBuilder
from agentTest.rag.schema_document_retriever import SchemaDocumentRetriever
from agentTest.rag.schema_snapshot_service import SchemaSnapshotService
from agentTest.schema.schema_context_builder import SchemaContextBuilder
from agentTest.config.tools import TOOLS
from agentTest.conversation import Conversation
from agentTest.llm import LLM
from agentTest.state.agent_state import AgentState
from agentTest.tools.sql_query_tool import SQLQueryTool
from agentTest.registry.tool_registry import ToolRegistry
from agentTest.prompt_builder import PromptBuilder
from agentTest.executor.executor import Executor
from agentTest.shceduler import Scheduler
from agentTest.utils.run_dumper import dump_run
from agentTest.tools.schema_tool import SchemaTool
from agentTest.services.planning_service import PlanningService
from agentTest.services.execution_service import ExecutionService
from agentTest.services.answer_service import AnswerService
import dotenv
import os


dotenv.load_dotenv()


class Agent:

    def __init__(self):
        # 1. 初始化基础运行组件：
        # - llm 负责调用模型
        # - conversation 保存对话过程
        # - state 保存本轮执行状态
        # - prompt_builder 负责构造 planner / answer 提示词
        self.llm = LLM()
        self.conversation = Conversation()
        self.state = AgentState()
        self.prompt_builder = PromptBuilder()

        # 2. 根据环境变量选择当前数据源和元数据提供者：
        # - Hive 模式：真实 Hive 查询 + Hive metadata
        # - MySQL 模式：MySQL 查询 + MySQL metadata
        data_source_type = os.getenv("DATA_SOURCE_TYPE", "mysql").lower()
        if data_source_type == "hive":
            self.datasource = HiveDataSource()
            self.metadata_provider = HiveMetadataProvider()
        else:
            self.datasource = MySQLDataSource()
            self.metadata_provider = MySQLMetadataProvider()

        # 3. 初始化 schema 能力组件：
        # - SchemaTool：元数据访问适配层
        # - SchemaContextBuilder：生成结构化 schema_context
        # - SchemaDocumentBuilder / Snapshot / Retriever：生成并检索 schema_rag_context
        self.schema_tool = SchemaTool(self.metadata_provider)
        self.schema_context_builder = SchemaContextBuilder(self.schema_tool)
        self.schema_document_builder = SchemaDocumentBuilder()
        self.schema_snapshot_service = SchemaSnapshotService(self.schema_tool, self.schema_document_builder)
        self.schema_document_retriever = SchemaDocumentRetriever()

        # 4. 初始化工具执行链路：
        # - SQLQueryTool：执行只读 SQL
        # - ToolRegistry：按工具名注册和路由工具
        # - Executor：执行单个计划步骤
        self.sql_query_tool = SQLQueryTool(self.datasource)
        self.tool_registry = ToolRegistry(TOOLS)
        self.tool_registry.register("sql_query", self.sql_query_tool)
        self.tool_registry.register("mysql_query", self.sql_query_tool)
        self.executor = Executor(self.tool_registry)

        # 5. 初始化阶段服务：
        # - PlanningService：生成 validated plan
        # - ExecutionService：执行 validated plan 并回写 state
        # - AnswerService：基于 trace 生成最终回答
        self.scheduler = Scheduler()
        self.planning_service = PlanningService(self.llm, self.prompt_builder, TOOLS)
        self.execution_service = ExecutionService(self.scheduler, self.executor, self.conversation)
        self.answer_service = AnswerService(self.llm, self.prompt_builder)

    def _build_schema_rag_context(self, query: str):
        # 输入：
        # - query：用户问题
        # 输出：
        # - retrieved_documents：最相关的 schema documents 列表
        schema_documents = self.schema_snapshot_service.build_snapshot()
        retrieved_documents = self.schema_document_retriever.retrieve(query, schema_documents, top_k=3)
        return retrieved_documents

    def chat(self, text):
        # 输入：
        # - text：用户问题
        # 输出：
        # - answer：最终自然语言回答

        # 0. 初始化当前轮次状态，避免上一轮对话污染本轮执行
        self.state.reset_run()
        self.state.query = text

        # 1. 生成结构化 schema 上下文，供 planner 精确选择候选表和字段
        schema_context = self.schema_context_builder.build(text)
        self.state.schema_context = schema_context

        # 2. 生成 schema RAG 上下文，供 planner 获得文档型 grounding 信息
        self.state.schema_rag_context = self._build_schema_rag_context(text)
        print(f"Schema RAG 命中文档数: {len(self.state.schema_rag_context)}")

        # 3. 调用 PlanningService 生成 validated plan
        response, validated_plan = self.planning_service.generate_plan(
            query=text,
            schema_context=schema_context,
            schema_rag_context=self.state.schema_rag_context
        )
        self.conversation.add_user(text)
        self.conversation.add_assistant(response)
        self.state.current_plan = validated_plan

        # 4. 调用 ExecutionService 按依赖执行计划步骤，并原地更新 state
        self.execution_service.run(validated_plan, self.state)

        # 5. 调用 AnswerService 基于执行 trace 生成最终回答
        answer = self.answer_service.answer(self.state)

        # 6. 落盘本轮运行记录，便于后续回放和排查问题
        dump_run(
            query=self.state.query,
            trace=self.state.trace,
            schema_context=self.state.schema_context,
            schema_rag_context=self.state.schema_rag_context,
            plan=self.state.current_plan,
            answer=answer
        )
        return answer
