#LLM
from openai import OpenAI
import contextvars
import os
import dotenv

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_enable_thinking
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_llm_call
from agentTest.langgraph_app.runtime.graph_logger import log_llm_error
from agentTest.langgraph_app.runtime.graph_logger import log_llm_response
from agentTest.langgraph_app.runtime.graph_logger import start_timer

dotenv.load_dotenv()

# 简要注释：当前请求的 LLM 调用方标签，节点开头设置，避免共享 LLM 实例属性被并发覆盖
_LLM_CALLER = contextvars.ContextVar("llm_caller", default="")


def set_llm_caller(caller: str):
    # 简要注释：标记后续 chat/invoke 的日志调用方（如 generate_sql / build_final_answer）
    _LLM_CALLER.set(caller)


class LLM:
    def __init__(self):
        self.client = OpenAI(
            api_key=get_openai_api_key(),
            base_url=get_openai_base_url(),
        )
        self.model = get_model_name()
        # 简要注释：标记本次调用的业务方（如 generate_sql），写入 llm 日志
        self.caller = "llm"

    def chat(self, messages):
        request_kwargs = {
            "model": self.model,
            "messages": messages,
            # response_format={"type": "json_object"},
            "response_format": None,
        }
        # 简要注释：根据配置决定是否传入 enable_thinking（控制模型思考模式）
        enable_thinking = get_model_enable_thinking()
        if enable_thinking is not None:
            request_kwargs["extra_body"] = {"enable_thinking": enable_thinking}
        # 简要注释：调用方标签优先取 ContextVar，未设置时回退实例属性
        caller = _LLM_CALLER.get() or self.caller
        # 简要注释：记录 LLM 调用（仅用户输入，不记录系统提示词），便于 trace 回放
        prompt_lines = [
            f"{item.get('role', 'user')}: "
            f"{item.get('content', '')}"
            for item in messages
            if item.get("role") in ("user", "human")
        ]
        log_llm_call(caller, self.model, prompt_lines)
        timer = start_timer()
        try:
            response = self.client.chat.completions.create(**request_kwargs)
            content = response.choices[0].message.content or ""
            log_llm_response(caller, self.model, content, ms=elapsed_ms(timer))
            return content
        except Exception as error:
            log_llm_error(caller, self.model, str(error), ms=elapsed_ms(timer))
            raise



    # 简要注释：适配 LangChain Prompt 结果并复用现有 chat 调用。
    def invoke(self, prompt_value):
        messgaes = []

        # 简要注释：把 LangChain 消息对象转换成 OpenAI chat messages 格式。
        for message in prompt_value.messages:
            role = "user"
            if message.type == "system":
                role = "system"
            elif message.type == "human":
                role = "user"
            elif message.type == "ai":
                role = "assistant"

            messgaes.append({
                "role": role,
                "content": message.content,
            })

        return self.chat(messgaes)
