# LangGraph 多轮交互演示入口 —— Planner 每轮独立判定，Demo 层管理循环
from agentTest.langgraph_app.graphs.supervisor_graph import build_supervisor_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime
from agentTest.langgraph_app.runtime.graph_logger import log_round_separator
from agentTest.langgraph_app.runtime.graph_logger import log_user_input
from agentTest.config.advisor import MAX_DEMO_ADVISOR_TURNS


def run_demo():
    runtime = build_graph_runtime()
    app = build_supervisor_graph(runtime)
    config = {"configurable": {"thread_id": "demo-session-1"}} # tread_id用于区分不同会话，这里先写死
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

    original_question = current_question  # 当前话题的原始问题
    advisor_turns = 0  # 同一话题内 Advisor 已追问轮数
    round_num = 1  # 全局轮次计数

    while True:
        # 用户简略回复（"1"\"a"\"好的"等）→ 用原始问题保证 Planner 语义完整
        question_for_planner = current_question
        if len(current_question.strip()) <= 5:
            question_for_planner = original_question

        # 轮次分隔线
        log_round_separator(round_num)
        # 记录本轮用户输入
        log_user_input(current_question)

        # 每轮一次完整图执行：Planner → Advisor 或 Seeker → END
        result = app.invoke({
                    "question": question_for_planner,
                    "original_question": original_question,
                },
                config)

        route = result.get("route", "seeker")
        round_num += 1

        if route == "advisor":
            # Advisor 给出了追问/建议，展示给用户
            print(f"\nAI: {result.get('final_answer', '')}")
            advisor_turns += 1

            # 硬止损：追问超过上限，提示用户重新描述
            if advisor_turns >= MAX_DEMO_ADVISOR_TURNS:
                print("\nAI: 抱歉，经过多轮沟通我仍无法确定您的需求，请尝试重新描述。")
                advisor_turns = 0
                current_question = input("\n你: ").strip()
                if not current_question or current_question.lower() in ("exit", "quit"):
                    print("结束对话。")
                    break
                original_question = current_question
                continue

            # 等待用户下一轮输入
            current_question = input("\n你: ").strip()
            if not current_question or current_question.lower() in ("exit", "quit"):
                print("结束对话。")
                break
            continue

        # route == "seeker"：Seeker 已生成最终答案
        print(f"\nAI: {result.get('final_answer', '')}")
        advisor_turns = 0  # 新话题，重置计数

        current_question = input("\n你: ").strip()
        if not current_question or current_question.lower() in ("exit", "quit"):
            print("结束对话。")
            break
        original_question = current_question


if __name__ == "__main__":
    run_demo()
