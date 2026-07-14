import os
from dotenv import load_dotenv
from openai import OpenAI
from agentTest.agent import Agent
import logging
from pathlib import Path


# 加载环境变量
load_dotenv()


def setup_logging():
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agent.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        filename=str(log_file),
        filemode="a",
        encoding="utf-8",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def main():
    setup_logging()
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

