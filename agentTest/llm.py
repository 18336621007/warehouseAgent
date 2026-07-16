#LLM
from openai import OpenAI
import os
import dotenv

dotenv.load_dotenv()

class LLM:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
        self.model = os.getenv("MODEL_NAME")

    def chat(self, messages):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            # response_format={"type": "json_object"},
            response_format=None

        )
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
