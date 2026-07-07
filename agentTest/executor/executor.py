from agentTest.state.step_status import StepStatus
from agentTest.tools.tool_router import ToolRouter


class Executor:

    def __init__(self, tool_registry):
        self.tool_registry = tool_registry
        self.tool_router = ToolRouter()

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
        step_id = plan_step.id
        step_name = plan_step.name
        tool = plan_step.tool
        if not tool:
            raise Exception(f"step {step_id} missing tool")

        args = self.build_args(plan_step, state)

        try:
            result = self.tool_registry.execute(tool, args)
            state.step_status[step_id] = StepStatus.SUCCESS
            return {
                "step_id": step_id,
                "name": step_name,
                "tool": tool,
                "input_args": args,
                "output": result,
                "error": None,
                "status": StepStatus.SUCCESS,
            }
        except Exception as error:
            state.step_retry[step_id] = state.step_retry.get(step_id, 0) + 1
            if state.step_retry[step_id] < 3:
                state.step_status[step_id] = StepStatus.RETRY
                return {
                    "step_id": step_id,
                    "name": step_name,
                    "tool": tool,
                    "input_args": args,
                    "output": None,
                    "error": str(error),
                    "status": StepStatus.RETRY,
                }

            state.step_status[step_id] = StepStatus.FAILED
            return {
                "step_id": step_id,
                "name": step_name,
                "tool": tool,
                "input_args": args,
                "output": None,
                "error": str(error),
                "status": StepStatus.FAILED,
            }
