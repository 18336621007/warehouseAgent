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
        self.llm = LLM()
        self.conversation = Conversation()
        self.state = AgentState()

        self.prompt_builder = PromptBuilder()
        self.scheduler = Scheduler()
        # 数据源初始化前先读取环境变量，保证测试和主程序行为一致
        data_source_type = os.getenv("DATA_SOURCE_TYPE", "mysql").lower()
        if data_source_type == "hive":
            self.datasource = HiveDataSource()
            self.metadata_provider = HiveMetadataProvider()
        else:
            self.datasource = MySQLDataSource()
            self.metadata_provider = MySQLMetadataProvider()

        # 声明工具
        self.sql_query_tool = SQLQueryTool(self.datasource)
        self.python_tool = PythonTool()
        self.schema_tool = SchemaTool(self.metadata_provider)
        self.schema_context_builder = SchemaContextBuilder(self.schema_tool)
        self.schema_document_builder = SchemaDocumentBuilder()
        self.schema_snapshot_service = SchemaSnapshotService(self.schema_tool, self.schema_document_builder)
        self.schema_document_retriever = SchemaDocumentRetriever()
        # 注册工具，同时兼容旧工具名，避免模型输出旧名时执行失败
        self.tool_registry = ToolRegistry(TOOLS)
        self.tool_registry.register("sql_query", self.sql_query_tool)
        self.executor = Executor(self.tool_registry)

    def execute_batch(self, steps, state):
        results = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = []

            for step in steps:
                futures.append(pool.submit(self.executor.execute, step, state))

            for future in futures:
                results.append(future.result())
        return results

    def observe(self, observation):
        self.conversation.add_assistant(
            f"step={observation['step_id']} status={observation['status']}"
        )

    def apply_observation(self, observation):
        """根据step结果更新状态，成功更新XCom记录快照，失败标记失败。最后记录trace"""
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
        # 先构建当前可见的 schema documents，再按 query 做检索
        schema_documents = self.schema_snapshot_service.build_snapshot()
        retrieved_documents = self.schema_document_retriever.retrieve(query, schema_documents, top_k=3)
        return retrieved_documents

    def chat(self, text):
        self.state.reset_run() # 清空状态，保证本轮对话的状态是干净的
        self.state.query = text

        # 0.获取候选元数据
        schema_context = self.schema_context_builder.build(text) # 获取候选表/列
        self.state.schema_context = schema_context
        self.state.schema_rag_context = self._build_schema_rag_context(text)
        print(f"Schema RAG 命中文档数: {len(self.state.schema_rag_context)}")

        # 1.生成plan提示词
        messages = self.prompt_builder.build_planner_prompt(
            query=text,
            tools=TOOLS,
            schema_context=schema_context,
            schema_rag_context=self.state.schema_rag_context
        )

        response = self.llm.chat(messages)
        self.conversation.add_user(text)
        self.conversation.add_assistant(response)
        plan = safe_parse_json(response)
        validated_plan = validate_plan(plan)
        self.state.current_plan = validated_plan
        self.scheduler.init(validated_plan, self.state)

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

        answer_messages = self.prompt_builder.build_answer_prompt(self.state)
        answer = self.llm.chat(answer_messages)
        dump_run(
            query=self.state.query,
            trace=self.state.trace,
            schema_context=self.state.schema_context,
            schema_rag_context=self.state.schema_rag_context,
            plan=self.state.current_plan,
            answer=answer
        )
        return answer
