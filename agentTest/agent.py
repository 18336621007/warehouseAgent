from agentTest.state.step_status import StepStatus
from agentTest.state.xcom_record import XComRecord
from agentTest.validate.safe_parse_json import safe_parse_json
from agentTest.validate.validator import validate_plan
from config.tools import TOOLS
from conversation import Conversation
from llm import LLM
from agentTest.state.state import AgentState
from tools.mysql_tool import MySQLTool
from tools.python_tool import PythonTool
from agentTest.registry.tool_registry import ToolRegistry
from prompt_builder import PromptBuilder
from executor.executor import Executor
from shceduler import Scheduler
from concurrent.futures import ThreadPoolExecutor


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

    def run_execution_loop(self):
        results = []
        max_round = 100
        round_count = 0
        while round_count < max_round:
            batch = self.scheduler.get_ready_batch(self.state)
            if not batch:
                break

            observations = self.execute_batch(batch, self.state)

            for obs in observations:
                self.observe(obs)

                if obs["status"] == StepStatus.FAILED:
                    self.scheduler.mark_failed_propagation(obs["step_id"], self.state)

                if obs["status"] == StepStatus.SUCCESS:
                    record = XComRecord(
                        step_id=obs["step_id"],
                        tool=obs["tool"],
                        input_args=obs["input_args"],
                        output=obs["output"],
                        status=obs["status"],
                    )
                    self.state.xcom[obs["step_id"]] = record

                self.state.trace.append({
                    "step_id": obs["step_id"],
                    "name": obs["name"],
                    "tool": obs["tool"],
                    "input_args": obs["input_args"],
                    "output": obs["output"],
                    "error": obs["error"],
                    "status": obs["status"]
                })

                results.append({
                    "step": obs["step_id"],
                    "name": obs["name"],
                    "tool": obs["tool"],
                    "result": obs["output"],
                    "error": obs["error"],
                    "status": obs["status"]
                })
            round_count += 1

        return results

    def chat(self, text):
        self.conversation.add_user(text)

        messages = self.prompt_builder.build_planner_prompt(
            query=text,
            tools=TOOLS
        )
        plan_raw = self.llm.chat(messages)
        plan = safe_parse_json(plan_raw)
        plan = validate_plan(plan)
        self.state.current_plan = plan

        self.scheduler.init(plan, self.state)
        results = self.run_execution_loop()

        summary_prompt = self.prompt_builder.build_summary_prompt(results)
        answer = self.llm.chat(summary_prompt)

        self.conversation.add_assistant(answer)

        return answer
