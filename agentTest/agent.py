from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.datasource.mysql_datasource import MySQLDataSource
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.metadata.mysql_metadata_provider import MySQLMetadataProvider
from agentTest.rag.schema_document_builder import SchemaDocumentBuilder
from agentTest.rag.schema_document_retriever import SchemaDocumentRetriever
from agentTest.rag.schema_snapshot_service import SchemaSnapshotService
from agentTest.schema.schema_context_builder import SchemaContextBuilder
from agentTest.state.step_status import StepStatus
from agentTest.state.xcom_record import XComRecord
from agentTest.validate.safe_parse_json import safe_parse_json
from agentTest.validate.validator import validate_plan
from agentTest.config.tools import TOOLS
from agentTest.conversation import Conversation
from agentTest.llm import LLM
from agentTest.state.agent_state import AgentState
from agentTest.tools.sql_query_tool import SQLQueryTool
from agentTest.tools.python_tool import PythonTool
from agentTest.registry.tool_registry import ToolRegistry
from agentTest.prompt_builder import PromptBuilder
from agentTest.executor.executor import Executor
from agentTest.shceduler import Scheduler
from concurrent.futures import ThreadPoolExecutor
from agentTest.utils.run_dumper import dump_run
import dotenv
import logging
import os
from agentTest.tools.schema_tool import SchemaTool


dotenv.load_dotenv()
logger = logging.getLogger(__name__)


class Agent:

    def __init__(self):
        # 1. 初始化基础运行组件：
        # - llm 负责调用模型
        # - conversation 保存对话过程
        # - state 保存本轮执行状态
        # - prompt_builder 负责构造 planner / answer 提示词
        # - scheduler 负责按依赖调度步骤
        self.llm = LLM()
        self.conversation = Conversation()
        self.state = AgentState()

        self.prompt_builder = PromptBuilder()
        self.scheduler = Scheduler()

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
        # - SchemaContextBuilder：生成候选表/字段上下文
        # - SchemaDocumentBuilder：把表结构转成 schema documents
        # - SchemaSnapshotService：批量构建 schema 文档快照
        # - SchemaDocumentRetriever：按 query 检索相关 schema 文档
        self.schema_tool = SchemaTool(self.metadata_provider)
        self.schema_context_builder = SchemaContextBuilder(self.schema_tool)
        self.schema_document_builder = SchemaDocumentBuilder()
        self.schema_snapshot_service = SchemaSnapshotService(self.schema_tool, self.schema_document_builder)
        self.schema_document_retriever = SchemaDocumentRetriever()

        # 4. 初始化工具执行链路：
        # - SQLQueryTool：执行只读 SQL
        # - PythonTool：执行 Python 数据处理逻辑
        # - ToolRegistry：按工具名注册和路由工具
        # - Executor：执行单个计划步骤
        self.sql_query_tool = SQLQueryTool(self.datasource)
        self.python_tool = PythonTool()
        self.tool_registry = ToolRegistry(TOOLS)

        # 当前先保留 sql_query 和 mysql_query 的兼容注册，
        # 避免模型偶尔输出旧工具名时直接导致执行失败。
        self.tool_registry.register("sql_query", self.sql_query_tool)
        self.tool_registry.register("mysql_query", self.sql_query_tool)
        self.tool_registry.register("python_tool", self.python_tool)
        self.executor = Executor(self.tool_registry)

    def execute_batch(self, steps, state):
        # 并发执行当前批次 ready step，并收集所有 observation 结果
        results = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = []

            for step in steps:
                futures.append(pool.submit(self.executor.execute, step, state))

            for future in futures:
                results.append(future.result())
        return results

    def observe(self, observation):
        # 将步骤执行状态写入对话记录，便于查看执行过程
        self.conversation.add_assistant(
            f"step={observation['step_id']} status={observation['status']}"
        )

    def apply_observation(self, observation):
        """将单个 observation 回写到全局 state：
        - 更新 step_status
        - 成功时写入 xcom
        - 失败时做失败传播
        - 所有 observation 追加到 trace
        """
        step_id = observation["step_id"]
        status = observation["status"]

        self.state.step_status[step_id] = status

        if status == StepStatus.SUCCESS:
            record = XComRecord(
                step_id=observation["step_id"],
                tool=observation["tool"],
                input_args=observation["input_args"],
                output=observation["output"],
                status=observation["status"],
            )
            self.state.xcom[step_id] = record
            logger.info(
                f"[STEP_END] step_id={step_id} status={status} tool={observation['tool']} duration_ms={observation['duration_ms']}")

        elif status == StepStatus.FAILED:
            self.scheduler.mark_failed_propagation(step_id, self.state)
            error = observation["error"]
            if error is None:
                error_type = None
            elif "missing dependency output" in error:
                error_type = "dependency_error"
            else:
                error_type = "tool_execution_error"
            logger.error(
                f"[STEP_FAILED] step_id={step_id} error_type={error_type} error={error}")
        elif status == StepStatus.RETRY:
            logger.warning(
                f"[STEP_RETRY] step_id={step_id} retry_count={observation['retry_count']}")

        self.state.trace.append(observation)

    def _build_schema_rag_context(self, query: str):
        # 根据当前问题生成 schema RAG 上下文：
        # 1. 构建当前可见表的 schema documents 快照
        # 2. 从 documents 中检索最相关的 top_k 文档
        # 3. 返回给 planner prompt 使用
        schema_documents = self.schema_snapshot_service.build_snapshot()
        retrieved_documents = self.schema_document_retriever.retrieve(query, schema_documents, top_k=3)
        return retrieved_documents

    def chat(self, text):
        # 0. 初始化当前轮次状态，避免上一轮对话污染本轮执行
        self.state.reset_run() # 清空状态，保证本轮对话的状态是干净的
        self.state.query = text

        # 1. 生成结构化 schema 上下文：
        # - 基于 metadata 召回候选表
        # - 基于候选表结构召回候选字段
        # - 结果供 planner 选择表和字段时参考
        schema_context = self.schema_context_builder.build(text) # 获取候选表/列
        self.state.schema_context = schema_context

        # 2. 生成 schema RAG 上下文：
        # - 先构建 schema documents
        # - 再按 query 检索最相关文档
        # - 结果作为 planner prompt 的补充 grounding 信息
        self.state.schema_rag_context = self._build_schema_rag_context(text)
        print(f"Schema RAG 命中文档数: {len(self.state.schema_rag_context)}")

        # 3. 构造 planner prompt，并调用 LLM 生成计划原文
        messages = self.prompt_builder.build_planner_prompt(
            query=text,
            tools=TOOLS,
            schema_context=schema_context,
            schema_rag_context=self.state.schema_rag_context
        )

        response = self.llm.chat(messages)
        self.conversation.add_user(text)
        self.conversation.add_assistant(response)

        # 4. 解析并校验 planner 输出：
        # - safe_parse_json：把模型文本解析成 JSON
        # - validate_plan：过滤非法 step，转成标准 PlanStep
        # - scheduler.init：初始化调度状态
        plan = safe_parse_json(response)
        validated_plan = validate_plan(plan)
        self.state.current_plan = validated_plan
        self.scheduler.init(validated_plan, self.state)

        # 5. 按依赖调度并执行计划步骤：
        # - scheduler 找出当前可执行的 ready step
        # - executor 并发执行这些 step
        # - observation 回写 state，驱动下游步骤解锁
        while True:
            ready_steps = self.scheduler.get_ready_batch(self.state)
            if not ready_steps:
                break

            observations = self.execute_batch(ready_steps, self.state)
            for observation in observations:
                self.observe(observation)
                self.apply_observation(observation)

            if self.scheduler.all_steps_finished(self.state):
                break

        # 6. 基于执行 trace 构造总结提示词，并调用 LLM 生成最终回答
        answer_messages = self.prompt_builder.build_answer_prompt(self.state)
        answer = self.llm.chat(answer_messages)

        # 7. 落盘本轮运行记录，便于后续回放和排查问题
        dump_run(
            query=self.state.query,
            trace=self.state.trace,
            schema_context=self.state.schema_context,
            schema_rag_context=self.state.schema_rag_context,
            plan=self.state.current_plan,
            answer=answer
        )
        return answer
