from agentTest.state.step_status import StepStatus
# from agentTest.tools.tool_router import ToolRouter
import time
import logging
logger = logging.getLogger(__name__)

class Executor:

    def __init__(self, tool_registry):
        self.tool_registry = tool_registry
        # self.tool_router = ToolRouter()

    def build_args(self, plan_step, state):
        args = {}

        for key, value in plan_step.inputs.items():
            if isinstance(value, str) and value.startswith("s"):
                parts = value.split(".", 1)
                step_id = parts[0]
                field = parts[1] if len(parts) > 1 else None
                xcom = state.xcom.get(step_id)
                if xcom is None:
                    raise Exception(f"missing dependency output: {step_id}")

                if field:
                    args[key] = xcom.output[field]
                else:
                    args[key] = xcom.output
            else:
                args[key] = value

        return args

    def execute(self, plan_step, state):
        """
        执行单个计划步骤，完成参数构建、工具调用、异常重试与状态封装

        逻辑说明：
        1. 提取步骤ID、名称、绑定工具标识，无工具则直接抛出异常；
        2. 根据步骤与全局状态构建工具入参；
        3. 调用工具注册表执行对应工具，成功返回SUCCESS状态结果；
        4. 执行捕获异常：记录重试次数，不足3次标记RETRY等待重跑；达到3次则标记FAILED永久失败。

        Args:
            plan_step: PlanStep 计划步骤对象，包含id/name/tool等基础信息
            state: 全局工作流状态，存储各步骤重试计数 step_retry

        Returns:
            dict: 标准化步骤执行结果，包含step_id、name、tool、入参、输出、错误信息、执行状态
            状态枚举：StepStatus.SUCCESS / StepStatus.RETRY / StepStatus.FAILED

        Raises:
            Exception: 当前步骤未配置tool工具标识，无法执行
        """
        #TODO 1. 提取步骤ID、名称、绑定工具标识，无工具则直接抛出异常；
        step_id = plan_step.id
        step_name = plan_step.name
        tool = plan_step.tool
        inputs = plan_step.inputs
        start_time = time.time()
        logger.info(
            f"[STEP_START] step_id={step_id} name={step_name} tool={tool} inputs = {inputs} start_time={start_time}"
        )
        if not tool:
            raise Exception(f"step {step_id} missing tool")
        #TODO 2. 根据步骤与全局状态构建工具入参
        args = self.build_args(plan_step, state)

        #TODO 3. 调用工具注册表执行对应工具，成功返回SUCCESS状态结果
        try:
            result = self.tool_registry.execute(tool, args)
            duration_ms = round((time.time() - start_time) * 1000, 2)
            return {
                "step_id": step_id,
                "name": step_name,
                "tool": tool,
                "input_args": args,
                "output": result,
                "error": None,
                "status": StepStatus.SUCCESS,
                "duration_ms": duration_ms,
                "retry_count": state.step_retry.get(step_id, 0)
            }
        # TODO 4. 执行捕获异常：记录重试次数，不足3次标记RETRY等待重跑；达到3次则标记FAILED永久失败。
        except Exception as error:
            retry_count = state.step_retry.get(step_id, 0) + 1
            state.step_retry[step_id] = retry_count
            if retry_count < 3:
                duration_ms = round((time.time() - start_time) * 1000, 2)
                return {
                    "step_id": step_id,
                    "name": step_name,
                    "tool": tool,
                    "input_args": args,
                    "output": None,
                    "error": str(error),
                    "status": StepStatus.RETRY,
                    "duration_ms": duration_ms,
                    "retry_count": retry_count
                }
            else:
                duration_ms = round((time.time() - start_time) * 1000, 2)
                return {
                    "step_id": step_id,
                    "name": step_name,
                    "tool": tool,
                    "input_args": args,
                    "output": None,
                    "error": str(error),
                    "status": StepStatus.FAILED,
                    "duration_ms": duration_ms,
                    "retry_count": retry_count
                }
