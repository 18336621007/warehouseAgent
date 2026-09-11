# 执行链兜底节点：SQL 校验失败或执行失败（重试耗尽）后给用户确定性错误文本
# A1：build_final_answer 删除后，失败路径由程序直接产出用户可见错误，不消耗额外 LLM 轮次。
from langchain_core.messages import AIMessage

from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState


def query_error_fallback_node(state: AgentState):
    """SQL 校验失败或执行失败且重试耗尽：直接给出用户可见错误文本并结束。"""
    timer = start_timer()
    log_node_start("query_error_fallback")

    sql_valid = state.get("sql_valid", False)
    if not sql_valid:
        message = "本次未执行 SQL 查询，因为生成的 SQL 未通过校验。原因：" + str(
            state.get("sql_error") or "SQL 校验失败"
        )
    elif state.get("sql_exec_failed"):
        message = "SQL 执行失败：" + str(state.get("sql_exec_error") or "未知执行错误")
    else:
        message = "当前查询未能完成，请稍后重试。"

    update = {
        "final_answer": message,
        "topic_status": "failed",
        "messages": [
            AIMessage(
                content=message,
                name="seeker",
                id=f"{state.get('request_id', '')}:seeker",
            )
        ],
        # 执行链兜底：不触发 Evaluator、不回 Planner 评审
        "execution_review": False,
        "evaluator_pending": False,
    }
    log_node_end("query_error_fallback", error=message[:120], ms=elapsed_ms(timer))
    log_state_snapshot("query_error_fallback", {**state, **update})
    return update
