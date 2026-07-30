# LangGraph 多轮交互演示入口 —— Planner 每轮独立判定，Demo 层管理循环
import uuid

from agentTest.langgraph_app.graphs.supervisor_graph import build_supervisor_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime
from agentTest.langgraph_app.runtime.graph_logger import log_round_separator
from agentTest.langgraph_app.runtime.graph_logger import log_user_input
from agentTest.config.advisor import MAX_DEMO_ADVISOR_TURNS


def run_demo():
    runtime = build_graph_runtime()
    app = build_supervisor_graph(runtime)
    conversation_id = "cli-demo"
    topic_id = uuid.uuid4().hex

    config = {
        "configurable": {
            "thread_id": f"{conversation_id}:{topic_id}"
        }
    }
    print("=" * 60)
    print("  欢迎使用智能数仓助手")
    print("  输入 exit 或 quit 退出对话")
    print("  有什么我可以帮到你的吗？")
    print("=" * 60)
    print()

    current_question = input("你: ").strip()
    if not current_question or current_question.lower() in ("exit", "quit"):
        print("结束对话。")
        return

    round_num = 1

    while True:
        log_round_separator(round_num)
        log_user_input(current_question)

        result = app.invoke({
                    "conversation_id": conversation_id,  # CLI运行期间保持同一个完整对话
                    "topic_id": topic_id,                # 当前独立查数问题标识
                    "request_id": uuid.uuid4().hex,      # 每轮调用使用独立请求标识
                    "current_user_input": current_question,  # 用户本轮真实输入，Planner/Advisor 据此理解意图
                },
                config)

        route = result.get("route", "seeker")
        round_num += 1

        if route == "advisor":
            print(f"\nAI: {result.get('final_answer', '')}")

            # Advisor轮次由Graph State自动累积
            advisor_turns = result.get("advisor_turns", 0)

            if advisor_turns >= MAX_DEMO_ADVISOR_TURNS:
                print("\nAI: 抱歉，经过多轮沟通我仍无法确定您的需求，请尝试重新描述。")
                current_question = input("\n你: ").strip()
                if not current_question or current_question.lower() in ("exit", "quit"):
                    print("结束对话。")
                    break
                # 超过追问上限后，新问题使用独立Topic和Checkpoint
                topic_id = uuid.uuid4().hex
                config = {
                    "configurable": {
                        "thread_id": f"{conversation_id}:{topic_id}"
                    }
                }
                continue

            current_question = input("\n你: ").strip()
            if not current_question or current_question.lower() in ("exit", "quit"):
                print("结束对话。")
                break
            continue

        # seeker：已生成最终答案
        print(f"\nAI: {result.get('final_answer', '')}")

        current_question = input("\n你: ").strip()
        if not current_question or current_question.lower() in ("exit", "quit"):
            print("结束对话。")
            break
        # Seeker完成后，下一问题使用独立Topic和Checkpoint
        topic_id = uuid.uuid4().hex
        config = {
            "configurable": {
                "thread_id": f"{conversation_id}:{topic_id}"
            }
        }


if __name__ == "__main__":
    run_demo()
