#LLM
from openai import OpenAI
import os
import dotenv

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_enable_thinking

dotenv.load_dotenv()

class LLM:
    def __init__(self):
        self.client = OpenAI(
            api_key=get_openai_api_key(),
            base_url=get_openai_base_url(),
        )
        self.model = get_model_name()

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
        response = self.client.chat.completions.create(**request_kwargs)
        return response.choices[0].message.content



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
