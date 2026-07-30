from langchain_core.prompts import ChatPromptTemplate

from langchain_core.messages import AIMessage

from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState


def _build_answer_update(state, final_answer):
    # 将Seeker最终回答同时写入标准Topic消息记忆
    return {
        "final_answer": final_answer,
        "messages": [
            AIMessage(
                content=final_answer,
                name="seeker",
                id=f"{state['request_id']}:seeker",
            )
        ],
    }


def build_build_final_answer_node(runtime):
    llm = runtime["llm"]

    def build_final_answer_node(state: AgentState):

        sql_valid = state.get("sql_valid", False)
        question = state.get("original_question", "")
        timer = start_timer()

        # 记录节点开始日志
        log_node_start("build_final_answer", sql_valid=sql_valid)

        try:
            # sql 校验失败直接返回 error
            if not sql_valid:
                sql_error = state.get("sql_error", "SQL校验失败")

                # 记录校验失败分支日志
                log_node_end(
                    "build_final_answer",
                    branch="sql_invalid",
                    sql_error=sql_error,
                    ms=elapsed_ms(timer),
                )

                return _build_answer_update(
                    state,
                    f"本次未执行 SQL 查询，因为生成的 SQL 未通过校验。原因：{sql_error}",
                )

            # sql 校验成功
            sql_result = state.get("sql_result", {})
            row_count = sql_result.get("row_count", 0) if isinstance(sql_result, dict) else 0

            if row_count == 0:

                # 记录空结果分支日志
                log_node_end(
                    "build_final_answer",
                    branch="empty_result",
                    rows=row_count,
                    ms=elapsed_ms(timer),
                )

                return _build_answer_update(
                    state,
                    "SQL 已成功执行，但没有查询到符合条件的数据。",
                )

            prompt = ChatPromptTemplate.from_messages([
                (
                    "system",
                    "你是一个数据分析助手。请严格基于提供的 SQL 查询结果回答用户问题，不要编造信息。"
                ),
                (
                    "human",
                    "用户问题：\n{question}\n\nSQL 执行结果：\n{sql_result}"
                )
            ])

            prompt_value = prompt.invoke({
                "question": question,
                "sql_result": sql_result,
            })

            final_answer = llm.invoke(prompt_value)

            # 记录成功分支日志 —— 截断 answer 避免日志过长
            answer_preview = str(final_answer)
            log_node_end(
                "build_final_answer",
                branch="success",
                answer_preview=answer_preview,
                ms=elapsed_ms(timer),
            )
            return _build_answer_update(state, final_answer)
        except Exception as error:
            # 记录节点异常日志
            log_node_error(
                "build_final_answer",
                question=question,
                error=str(error),
                ms=elapsed_ms(timer),
            )
            raise

    return build_final_answer_node
