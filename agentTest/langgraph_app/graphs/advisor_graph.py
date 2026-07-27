# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
# 一问一答模式，不保留内部状态机。Planner 是唯一的调度中心。
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.state.advisor_state import AdvisorState
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms, log_node_event
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from agentTest.langgraph_app.state.agent_state import AgentState
from datetime import datetime


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

    graph = StateGraph(AdvisorState)

    def run_advisor(state):
        """处理用户问题：基于完整对话历史生成回复。
        用 user_response（用户原话）理解选择，如"1"/"月租订单"/"好的"；
        Planner 用 question 保证语义完整，两者互不干扰。"""
        question = state.get("user_response", state["question"])
        timer = start_timer()
        log_node_start("advisor_agent", question=question[:60])

        # 用标准 HumanMessage 追加用户本轮问题，Agent 能从历史中看到上下文
        history = list(state.get("advisor_messages") or [])
        history.append(HumanMessage(content=question))

        result = agent.invoke({"messages": history})

        # 直接用 result["messages"] 作为完整历史（Message 对象列表，含 tool_calls 等元数据）
        new_history = result["messages"]
        last_msg = new_history[-1]

        # ── 调试：打印 Agent 本轮所有 tool_calls ──
        all_tool_names = []
        for msg in new_history:
            for tc in (getattr(msg, "tool_calls", None) or []):
                all_tool_names.append(tc.get("name", "?"))
        log_node_event("advisor_agent", f"本轮工具调用: {all_tool_names if all_tool_names else '[无]'}")

        # ── 检测 confirm_selection 工具调用，写入独立的 confirmed_plan 字段 ──
        # confirmed_plan 是独立字段，不会被 Planner 覆盖，职责单一
        confirmed_plan = None
        for msg in new_history:
            for tc in (getattr(msg, "tool_calls", None) or []):
                if tc.get("name") == "confirm_selection":
                    confirmed_plan = {
                        "tables": [tc["args"]["table"]],
                        "fields": tc["args"]["fields"],
                        "confirmed_at": datetime.now().isoformat(),
                    }
                    # Agent 调用了工具 → 追加 ToolMessage，否则下一轮 Agent 认为工具调用未完成
                    new_history.append(ToolMessage(
                        content=f"已确认: {tc['args']}",
                        tool_call_id=tc["id"],
                    ))
                    break
            if confirmed_plan:
                break

        log_node_end("advisor_agent",
                     answer_summary=str(last_msg.content),
                     confirmed=confirmed_plan is not None,
                     ms=elapsed_ms(timer))

        return_value = {
            "final_answer": last_msg.content,
            "advisor_messages": new_history,
        }

        # 确认后写入独立字段 confirmed_plan（不再碰 planner_entities）
        if confirmed_plan:
            return_value["confirmed_plan"] = confirmed_plan

        return return_value

    graph.add_node("advisor_agent", run_advisor)
    graph.add_edge(START, "advisor_agent")
    graph.add_edge("advisor_agent", END)

    return graph.compile()
