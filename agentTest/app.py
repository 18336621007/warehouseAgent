import os
from dotenv import load_dotenv
from openai import OpenAI
from agent import Agent
#加载环境变量
load_dotenv()



def main():
    agent = Agent()

    print("智能数仓Agent已启动")
    print("输入exit退出")

    while True:
        question = input("\n你：")
        if question == "exit":
            break

        answer = agent.chat(question)

        print("\nAgent:", answer)

if __name__ == "__main__":
    main()