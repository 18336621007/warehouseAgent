# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
# 一问一答模式，不保留内部状态机。Planner 是唯一的调度中心。
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from agentTest.langgraph_app.state.agent_state import AgentState


def build_advisor_subgraph(runtime):
    """构建 Advisor ReAct Agent 子图 —— 一问一答，无内部状态"""
    llm = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,
    )

    tools = build_advisor_tools(
        runtime["db_vector_store"],
        runtime["table_vector_store"],
        runtime["column_vector_store"],
    )

    agent = create_agent(llm, tools, system_prompt=ADVISOR_SYSTEM_PROMPT)

    graph = StateGraph(AgentState)

    def run_advisor(state):
        """处理用户问题：基于完整对话历史（advisor_messages）生成回复"""
        question = state["question"]
        timer = start_timer()
        log_node_start("advisor_agent", question=question[:80])

        # 累积对话历史，让 Agent 理解用户的 "1"\"a\" 等简略回复
        history = state.get("advisor_messages") or []
        history.append({"role": "user", "content": question})

        result = agent.invoke({"messages": history})
        last_msg = result["messages"][-1]
        history.append({"role": "assistant", "content": last_msg.content})

        log_node_end("advisor_agent",
                     final_answer=last_msg.content,
                     duration_ms=elapsed_ms(timer))

        return {
            "final_answer": last_msg.content,
            "advisor_messages": history,
        }

    graph.add_node("advisor_agent", run_advisor)
    graph.add_edge(START, "advisor_agent")
    graph.add_edge("advisor_agent", END)

    return graph.compile()
