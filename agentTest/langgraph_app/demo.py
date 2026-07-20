# 简要注释：LangGraph 最小工作流演示入口，用于本地验证 graph 链路。

from agentTest.langgraph_app.graphs.sql_agent_graph import build_sql_agent_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime


# 简要注释：运行最小 SQL Agent Graph。
def run_demo(question: str):
    runtime = build_graph_runtime()
    app = build_sql_agent_graph(runtime)

    initial_state = {
        "question": question,
    }

    result = app.invoke(initial_state)
    return result


# 简要注释：本地示例入口。
if __name__ == "__main__":
    question = "昨天补贴金额的订单，给我一个"
    result = run_demo(question)
    print(result["final_answer"])
