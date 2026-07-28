# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
# 一问一答模式，不保留内部状态机。Planner 是唯一的调度中心。
# 企业级三层防护：Prompt 指引 → 图级拦截(硬保障) → 可观测日志
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.state.advisor_state import AdvisorState
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms, log_node_event
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from datetime import datetime
from agentTest.config.advisor import MAX_COLUMN_CHECK_RETRIES



def build_advisor_subgraph(runtime):
    """构建 Advisor ReAct Agent 子图 —— 一问一答，含 confirm_selection 合规校验"""
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

    def _find_tool_calls(messages, tool_name):
        """检查消息列表中是否包含指定工具的调用"""
        for msg in messages:
            for tc in (getattr(msg, "tool_calls", None) or []):
                if tc.get("name") == tool_name:
                    return True
        return False

    def run_advisor(state):
        """处理用户问题：基于完整对话历史生成回复。
        用 user_response（用户原话）理解选择，如"1"/"月租订单"/"好的"；
        Planner 用 question 保证语义完整，两者互不干扰。
        企业级防护：confirm_selection 前必须调过 search_columns，否则拦截重跑。"""
        question = state.get("user_response", state["question"])
        timer = start_timer()
        log_node_start("advisor_agent", question=question[:60])

        history = list(state.get("advisor_messages") or [])
        history.append(HumanMessage(content=question))

        new_history = None
        retries = 0

        while retries <= MAX_COLUMN_CHECK_RETRIES:
            result = agent.invoke({"messages": history})
            new_history = result["messages"]

            # ── 图级拦截：confirm_selection 前必须调过 search_columns ──
            # 仅在当前轮次（本次追加的 messages）中检查
            has_confirm = _find_tool_calls(new_history, "confirm_selection")
            has_column_search = _find_tool_calls(new_history, "search_columns")
            has_table_search = _find_tool_calls(new_history, "search_tables")

            if has_confirm and not has_column_search:
                retries += 1
                log_node_event("advisor_agent",
                    f"拦截 confirm_selection（缺少 search_columns），重试 {retries}/{MAX_COLUMN_CHECK_RETRIES}")
                # 注入系统提示，要求 Agent 先检索字段
                history.append(HumanMessage(
                    content="调用 confirm_selection 前必须先调用 search_columns 检索该表的所有字段。"
                            "请调用 search_columns，列出相关字段供用户选择，不要直接锁定方案。"
                ))
                continue  # 重新跑 Agent

            break  # 合规或重试次数耗尽

        if new_history is None:
            log_node_event("advisor_agent", "Agent 未返回有效结果")
            return {"final_answer": "系统处理异常，请重试", "advisor_messages": history}

        last_msg = new_history[-1]

        # ── 调试：打印 Agent 本轮所有 tool_calls ──
        all_tool_names = []
        for msg in new_history:
            for tc in (getattr(msg, "tool_calls", None) or []):
                all_tool_names.append(tc.get("name", "?"))
        log_node_event("advisor_agent", f"本轮工具调用: {all_tool_names if all_tool_names else '[无]'}")

        # ── 检测 confirm_selection 工具调用，写入独立的 confirmed_plan 字段 ──
        confirmed_plan = None
        for msg in new_history:
            for tc in (getattr(msg, "tool_calls", None) or []):
                if tc.get("name") == "confirm_selection":
                    confirmed_plan = {
                        "tables": [tc["args"]["table"]],
                        "fields": tc["args"]["fields"],
                        "confirmed_at": datetime.now().isoformat(),
                    }
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
                     retries=retries,
                     ms=elapsed_ms(timer))

        return_value = {
            "final_answer": last_msg.content,
            "advisor_messages": new_history,
        }

        if confirmed_plan:
            return_value["confirmed_plan"] = confirmed_plan

        return return_value

    graph.add_node("advisor_agent", run_advisor)
    graph.add_edge(START, "advisor_agent")
    graph.add_edge("advisor_agent", END)

    return graph.compile()
