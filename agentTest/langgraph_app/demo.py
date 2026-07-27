# LangGraph 多轮交互演示入口 —— Planner 每轮独立判定，Demo 层管理循环
from agentTest.langgraph_app.graphs.supervisor_graph import build_supervisor_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime
from agentTest.langgraph_app.runtime.graph_logger import log_round_separator
from agentTest.langgraph_app.runtime.graph_logger import log_user_input
from agentTest.config.advisor import MAX_DEMO_ADVISOR_TURNS


def run_demo():
    runtime = build_graph_runtime()
    app = build_supervisor_graph(runtime)
    config = {"configurable": {"thread_id": "demo-session-1"}}
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

    original_question = current_question
    advisor_turns = 0
    round_num = 1
    advisor_last_answer = ""

    while True:
        log_round_separator(round_num)
        log_user_input(current_question)

        result = app.invoke({
                    "question": original_question,      # 始终用原始问题，保持话题一致性
                    "original_question": original_question,
                    "user_response": current_question,   # 用户本轮真实输入，Planner/Advisor 据此理解意图
                    "advisor_last_answer": advisor_last_answer,
                    "advisor_turns": advisor_turns,      # Evaluator 用：累计追问轮次
                },
                config)

        route = result.get("route", "seeker")
        round_num += 1

        if route == "advisor":
            print(f"\nAI: {result.get('final_answer', '')}")
            advisor_last_answer = result.get("final_answer", "")
            advisor_turns += 1

            if advisor_turns >= MAX_DEMO_ADVISOR_TURNS:
                print("\nAI: 抱歉，经过多轮沟通我仍无法确定您的需求，请尝试重新描述。")
                advisor_turns = 0
                advisor_last_answer = ""
                current_question = input("\n你: ").strip()
                if not current_question or current_question.lower() in ("exit", "quit"):
                    print("结束对话。")
                    break
                original_question = current_question
                continue

            current_question = input("\n你: ").strip()
            if not current_question or current_question.lower() in ("exit", "quit"):
                print("结束对话。")
                break
            continue

        # seeker：已生成最终答案
        print(f"\nAI: {result.get('final_answer', '')}")
        advisor_turns = 0
        advisor_last_answer = ""

        current_question = input("\n你: ").strip()
        if not current_question or current_question.lower() in ("exit", "quit"):
            print("结束对话。")
            break
        original_question = current_question


if __name__ == "__main__":
    run_demo()
