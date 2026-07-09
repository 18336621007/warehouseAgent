from agentTest.schema.schema_context_builder import SchemaContextBuilder
from agentTest.state.step_status import StepStatus
from agentTest.state.xcom_record import XComRecord
from agentTest.validate.safe_parse_json import safe_parse_json
from agentTest.validate.validator import validate_plan
from config.tools import TOOLS
from conversation import Conversation
from llm import LLM
from agentTest.state.agent_state import AgentState
from tools.mysql_tool import MySQLTool
from tools.python_tool import PythonTool
from agentTest.registry.tool_registry import ToolRegistry
from prompt_builder import PromptBuilder
from executor.executor import Executor
from shceduler import Scheduler
from concurrent.futures import ThreadPoolExecutor
from agentTest.utils.run_dumper import dump_run
import logging


logger = logging.getLogger(__name__)

class Agent:

    def __init__(self):
        self.llm = LLM()
        self.conversation = Conversation()
        self.state = AgentState()
        self.mysql_tool = MySQLTool()
        self.python_tool = PythonTool()
        self.tool_registry = ToolRegistry(TOOLS)

        self.tool_registry.register("mysql_query", self.mysql_tool)
        self.tool_registry.register("python_tool", self.python_tool)

        self.prompt_builder = PromptBuilder()
        self.executor = Executor(self.tool_registry)
        self.scheduler = Scheduler()

        self.schema_context_builder = SchemaContextBuilder()

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
            # 简单异常分类
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
                f"[STEP_RETRY] step_id={step_id} retry_count={observation['retry_count']} error={observation['error']}")

        #简单异常分类
        error = observation["error"]
        if error is None:
            error_type = None
        elif "missing dependency output" in error:
            error_type = "dependency_error"
        else:
            error_type = "tool_execution_error"

        self.state.trace.append({
            "step_id": observation["step_id"],
            "name": observation["name"],
            "tool": observation["tool"],
            "input_args": observation["input_args"],
            "output": observation["output"],
            "error": observation["error"],
            "status": observation["status"],
            "retry_count": observation["retry_count"],
            "duration_ms": observation["duration_ms"],
            "error_type": error_type
        })



    def build_result_item(self, observation):
        return {
            "step_id": observation["step_id"],
            "name": observation["name"],
            "tool": observation["tool"],
            "output": observation["output"],
            "error": observation["error"],
            "status": observation["status"]
        }

    def build_run_summary(self):

        statuses = list(self.state.step_status.values())

        total_steps = len(statuses)
        success_steps = sum(1 for status in statuses if status == StepStatus.SUCCESS)
        failed_steps = sum(1 for status in statuses if status == StepStatus.FAILED)
        skipped_steps = sum(1 for status in statuses if status == StepStatus.SKIPPED)

        retry_steps = len({
            item["step_id"]
            for item in self.state.trace
            if item["status"] == StepStatus.RETRY
        })

        if failed_steps > 0:
            run_status = "failed"
        elif skipped_steps > 0:
            run_status = "partial_success"
        elif total_steps > 0 and success_steps == total_steps:
            run_status = "success"
        else:
            run_status = "running"

        return {
            "query": self.state.query,
            "total_steps": total_steps,
            "success_steps": success_steps,
            "failed_steps": failed_steps,
            "skipped_steps": skipped_steps,
            "retry_steps": retry_steps,
            "run_status": run_status
        }

    def run_execution_loop(self):
        """
            执行工作流主循环，批量调度、执行步骤并更新全局状态，限制最大执行轮次防止死循环

            执行逻辑：
            1. 循环获取当前就绪可执行的步骤批次；
            2. 无就绪批次时校验全部任务是否完成，未完成则抛出流程卡死异常；
            3. 批量执行就绪步骤，获取执行观测结果；
            4. 依次记录观测、更新状态、组装结果存入列表；
            5. 达到最大轮次或全部步骤执行完毕后退出循环，返回完整执行结果。

            Returns:
                list: 所有步骤执行完成后组装的结果条目列表

            Raises:
                RuntimeError: 无就绪步骤但仍存在未完成任务，工作流发生阻塞卡死
        """
        results = []
        max_round = 100 #最大轮次
        round_count = 0 #当前轮次
        while round_count < max_round:
            # 1. 获取当前就绪可执行的步骤批次
            batch = self.scheduler.get_ready_batch(self.state)

            # 2. 无就绪批次时校验全部任务是否完成，未完成则抛出流程卡死异常
            if not batch:
                if self.scheduler.all_steps_finished(self.state):
                    break

                unfinished_steps = self.scheduler.get_unfinished_steps(self.state)
                logger.error(
                    f"[RUN_STALLED] unfinished_steps={unfinished_steps}"
                )
                raise RuntimeError(f"workflow stalled, unfinished steps: {unfinished_steps}")

            # 3. 批量执行就绪步骤，获取执行观测结果
            observations = self.execute_batch(batch, self.state)

            #4.依次记录观测、更新状态、组装结果存入列表
            for obs in observations:
                self.observe(obs)
                self.apply_observation(obs)
                results.append(self.build_result_item(obs))

            round_count += 1

        return results

    def chat(self, text):
        self.state.reset_run() # 清空状态，保证本轮对话的状态是干净的
        self.state.query = text

        logger.info((f"[RUN_START] query={text}"))
        self.conversation.add_user(text) # 用户对话加入到conversation

        schema_context = self.schema_context_builder.build(text)
        self.state.schema_context = schema_context

        # 1.生成plan提示词
        messages = self.prompt_builder.build_planner_prompt(
            query=text,
            tools=TOOLS,
            schema_context = schema_context
        )
        # 2.LLM生成plan，解析为json，再解析为PlanStep列表
        plan_raw = self.llm.chat(messages) # 模型生成plan，预期是json
        plan_json = safe_parse_json(plan_raw) #把文本解析为json
        plan_step_list = validate_plan(plan_json) # 把json解析成PlanStep
        self.state.current_plan = plan_step_list
        logger.info(
            f"[PLAN_READY] step_count={len(plan_step_list)} "
            f"steps={[f'{step.id}:{step.name}' for step in plan_step_list]}"
        )

        # 3.初始化scheduler
        self.scheduler.init(plan_step_list, self.state)
        #4. 执行
        results = self.run_execution_loop()

        self.state.run_summary = self.build_run_summary()
        logger.info(f"[SUMMARY_INPUT] results={results}")
        summary_prompt = self.prompt_builder.build_summary_prompt(results)
        answer = self.llm.chat(summary_prompt)
        logger.info(f"[SUMMARY_OUTPUT] answer={answer}")
        logger.info(
            f"[RUN_END] run_status={self.state.run_summary['run_status']} "
            f"total_steps={self.state.run_summary['total_steps']} "
            f"success_steps={self.state.run_summary['success_steps']} "
            f"failed_steps={self.state.run_summary['failed_steps']} "
            f"skipped_steps={self.state.run_summary['skipped_steps']}"
        )

        # 将本轮运行信息落盘，便于后续 debug 和问题复盘
        dump_file = dump_run(
            query=self.state.query,
            schema_context=self.state.schema_context,
            current_plan=self.state.current_plan,
            trace=self.state.trace,
            run_summary=self.state.run_summary,
            answer=answer
        )
        logger.info(f"[RUN_DUMPED] file={dump_file}")

        self.conversation.add_assistant(answer)

        return answer
