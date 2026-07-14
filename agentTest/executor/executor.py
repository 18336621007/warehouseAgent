from agentTest.state.step_status import StepStatus
import time
import logging
logger = logging.getLogger(__name__)


class Executor:

    def __init__(self, tool_registry):
        self.tool_registry = tool_registry

    def _is_step_reference(self, value: str):
        # 只有形如 s1 或 s1.field 的字符串才视为上游步骤引用
        if not isinstance(value, str):
            return False
        if not value.startswith("s"):
            return False
        head = value.split(".", 1)[0]
        return len(head) > 1 and head[1:].isdigit()

    def build_args(self, plan_step, state):
        # 将 step 输入中的上游依赖引用替换成真实执行结果
        args = {}

        for key, value in plan_step.inputs.items():
            if self._is_step_reference(value):
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
        """
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

        args = self.build_args(plan_step, state)

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
