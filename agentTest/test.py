import os
from dotenv import load_dotenv
from openai import OpenAI
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
from dotenv import load_dotenv
from agentTest.tools.mysql_tool import MySQLTool

load_dotenv()
args = {"sql": ""}

mysql_tool = MySQLTool()
result = mysql_tool.run(args)
for row in result["rows"]:
    print(row,"\n")