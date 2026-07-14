from concurrent.futures import ThreadPoolExecutor
from agentTest.state.step_status import StepStatus
from agentTest.state.xcom_record import XComRecord
import logging

logger = logging.getLogger(__name__)


class ExecutionService:
    # ExecutionService 负责执行 validated plan：
    # - 初始化 scheduler
    # - 按依赖获取 ready step 批次
    # - 调用 executor 执行步骤
    # - 将 observation 回写到 state

    def __init__(self, scheduler, executor, conversation=None):
        # 输入：
        # - scheduler：步骤调度器
        # - executor：单步执行器
        # - conversation：可选，对话记录器，用于记录执行状态
        # 输出：
        # - 无直接输出，run 方法会原地更新 state
        self.scheduler = scheduler
        self.executor = executor
        self.conversation = conversation

    def execute_batch(self, steps, state):
        # 输入：
        # - steps：当前批次可执行的 step 列表
        # - state：全局执行状态
        # 输出：
        # - observations：本批次所有步骤执行结果
        results = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = []
            for step in steps:
                futures.append(pool.submit(self.executor.execute, step, state))

            for future in futures:
                results.append(future.result())
        return results

    def observe(self, observation):
        # 输入：
        # - observation：单个步骤执行结果
        # 输出：
        # - 无，副作用是向 conversation 追加执行痕迹
        if self.conversation is not None:
            self.conversation.add_assistant(
                f"step={observation['step_id']} status={observation['status']}"
            )

    def apply_observation(self, observation, state):
        # 输入：
        # - observation：单个步骤执行结果
        # - state：全局执行状态
        # 输出：
        # - 无，副作用是更新 step_status / xcom / trace
        step_id = observation["step_id"]
        status = observation["status"]

        state.step_status[step_id] = status

        if status == StepStatus.SUCCESS:
            record = XComRecord(
                step_id=observation["step_id"],
                tool=observation["tool"],
                input_args=observation["input_args"],
                output=observation["output"],
                status=observation["status"],
            )
            state.xcom[step_id] = record
            logger.info(
                f"[STEP_END] step_id={step_id} status={status} tool={observation['tool']} duration_ms={observation['duration_ms']}")
        elif status == StepStatus.FAILED:
            self.scheduler.mark_failed_propagation(step_id, state)
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

        state.trace.append(observation)

    def run(self, validated_plan, state):
        # 输入：
        # - validated_plan：已通过校验的 PlanStep 列表
        # - state：全局执行状态
        # 输出：
        # - state：原地更新后的执行状态对象
        self.scheduler.init(validated_plan, state)

        while True:
            ready_steps = self.scheduler.get_ready_batch(state)
            if not ready_steps:
                break

            observations = self.execute_batch(ready_steps, state)
            for observation in observations:
                self.observe(observation)
                self.apply_observation(observation, state)

            if self.scheduler.all_steps_finished(state):
                break

        return state
