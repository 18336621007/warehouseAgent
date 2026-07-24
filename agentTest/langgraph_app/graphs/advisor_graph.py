# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph

from agentTest.config.advisor import MAX_ADVISOR_ROUNDS
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from agentTest.langgraph_app.state.agent_state import AgentState
from langgraph.types import interrupt, Command
from langgraph.graph import StateGraph, START, END


def build_advisor_subgraph(runtime):
    """构建 Advisor ReAct Agent 子图"""
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

    # 状态适配：AgentState ← → Agent 内部 messages 格式
    graph = StateGraph(AgentState)

    def run_advisor(state):
        question = state["question"]
        advisor_round = state.get("advisor_round", 0) + 1
        timer = start_timer()
        log_node_start("advisor_agent", question=question[:80])

        # 累积对话历史，让 Agent 理解用户的 "1" 等简略回复
        history = state.get("advisor_messages") or []
        history.append({"role": "user", "content": question})

        result = agent.invoke({"messages": history})
        last_msg = result["messages"][-1]
        history.append({"role": "assistant", "content": last_msg.content})


        log_node_end("advisor_agent",
                     round=advisor_round,
                     answer_preview=last_msg.content[:120],
                     duration_ms=elapsed_ms(timer))

        return {
            "final_answer": last_msg.content,
            "advisor_round": advisor_round,
            "advisor_messages": history,  # 持久化对话历史
            "question": question # 显式传递，确保父图 state 同步
        }

    def wait_user_clarification(state):
        """暂停等待用户回答"""
        advisor_question = state.get("final_answer", "请进一步说明您的需求")
        # interrupt 暂停图执行，返回问题给调用方
        user_response = interrupt(advisor_question)
        # 用户回答后恢复，更新 question 为用户的澄清内容
        return {"question": user_response}

    # 路由：轮次超限 → 回父图让 Planner 重新判定
    def route_after_advisor_round(state):
        if state.get("advisor_round", 0) >= MAX_ADVISOR_ROUNDS:
            return "end"

        return "clarify"


    graph.add_node("advisor_agent", run_advisor)
    graph.add_node("wait_user", wait_user_clarification)


    graph.add_edge(START, "advisor_agent")
    # 条件路由：确认 → END（回父图）；追问 → wait_user（暂停）
    graph.add_conditional_edges(
        "advisor_agent",
        route_after_advisor_round,
        {
            "end": END,  # 已确认映射 → 返回父图
            "clarify": "wait_user",  # 需要澄清 → 暂停等用户
        }
    )
    # 用户回答后回到 advisor_agent 重新评估
    graph.add_edge("wait_user", "advisor_agent")

    return graph.compile()