# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.routers.advisor_router import route_after_advisor
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from agentTest.langgraph_app.state.agent_state import AgentState
from langgraph.types import interrupt, Command
from langgraph.graph import StateGraph, START, END

def _is_confirmed(final_answer: str) -> bool:
    """判断 Advisor 回复是否为「确认映射」而非「追问」"""
    # 确认特征：包含 (内部映射: ...) 格式
    return "(内部映射:" in final_answer

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
        messages = [{"role": "user", "content": question}]
        result = agent.invoke({"messages": messages})
        last_msg = result["messages"][-1]
        advisor_confirmed = _is_confirmed(last_msg.content)
        return {
            "final_answer": last_msg.content,
            "advisor_confirmed": advisor_confirmed,
        }

    def wait_user_clarification(state):
        """暂停等待用户回答"""
        advisor_question = state.get("final_answer", "请进一步说明您的需求")
        # interrupt 暂停图执行，返回问题给调用方
        user_response = interrupt(advisor_question)
        # 用户回答后恢复，更新 question 为用户的澄清内容
        return {"question": user_response}

    graph.add_node("advisor_agent", run_advisor)
    graph.add_node("wait_user", wait_user_clarification)


    graph.add_edge(START, "advisor_agent")
    # 条件路由：确认 → END（回父图）；追问 → wait_user（暂停）
    graph.add_conditional_edges(
        "advisor_agent",
        route_after_advisor,
        {
            "confirm": END,  # 已确认映射 → 返回父图
            "clarify": "wait_user",  # 需要澄清 → 暂停等用户
        }
    )
    # 用户回答后回到 advisor_agent 重新评估
    graph.add_edge("wait_user", "advisor_agent")

    return graph.compile()