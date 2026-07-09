import os
from dotenv import load_dotenv
from openai import OpenAI

from agentTest.tools.schema_tool import SchemaTool

# from agent import Agent
# from agentTest.llm import LLM
#
# #加载环境变量
# load_dotenv()
#
#
# def main():
#
#     print("智能数仓Agent已启动")
#     print("输入exit退出")
#     llm = LLM()
#
#     while True:
#         question = input("\n你：")
#         if question == "exit":
#             break
#
#         answer = llm.testchat(question)
#
#         print("\nLLM:", answer)
#
# if __name__ == "__main__":
