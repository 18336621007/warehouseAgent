#LLM
from openai import OpenAI
import os


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


    def testchat(self, question: str):
        # 关键：包裹成数组，带上role标识
        messages = [
            {"role": "user", "content": question}
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
        )
        return response.choices[0].message.content
