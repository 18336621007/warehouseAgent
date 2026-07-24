# LangGraph 多轮交互演示入口 —— 支持 Advisor interrupt 暂停/恢复（多轮）
from langgraph.types import Command

from agentTest.langgraph_app.graphs.supervisor_graph import build_supervisor_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime


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

    while True:
        # 发起首次图执行
        result = app.invoke({"question": current_question}, config)

        # 内层循环：处理连续的 interrupt（Advisor 可能多轮澄清）
        while True:
            interrupt_info = result.get("__interrupt__")
            if not interrupt_info:
                break  # 无中断，图执行完成

            # 显示 Advisor 的追问
            advisor_msg = interrupt_info[0].value if interrupt_info else "请进一步说明您的需求"
            print(f"\nAI: {advisor_msg}")

            user_answer = input("\n你: ").strip()
            if not user_answer or user_answer.lower() in ("exit", "quit"):
                print("结束对话。")
                return

            # 注入用户回答，恢复执行——可能再次中断
            result = app.invoke(Command(resume=user_answer), config)
            # 继续内层循环，检查是否有新的 interrupt

        # 无中断 = 图正常结束（seeker 生成了最终答案）
        final_answer = result.get("final_answer", "")
        print(f"\nAI: {final_answer}")

        current_question = input("\n你: ").strip()
        if not current_question or current_question.lower() in ("exit", "quit"):
            print("结束对话。")
            break


if __name__ == "__main__":
    run_demo()