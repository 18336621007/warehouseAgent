import datetime

from langchain_core.prompts import ChatPromptTemplate

from langchain_core.messages import AIMessage

from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.llm import set_llm_caller
from agentTest.langgraph_app.prompts.final_answer_prompt import (
    FINAL_ANSWER_HUMAN_TEMPLATE,
    FINAL_ANSWER_SYSTEM_PROMPT,
)
from agentTest.langgraph_app.state.agent_state import AgentState


# 结果快照只保存预览与引用，避免把全量结果写入 checkpoint
RESULT_PREVIEW_MAX_ROWS = 20
RESULT_ENTITY_KEYS_MAX = 50


def _build_result_snapshot(state, sql_result):
    """查询成功后生成结构化结果快照（QueryResultSnapshot）：引用+预览+实体键。"""
    columns = list((sql_result or {}).get("columns") or [])
    rows = list((sql_result or {}).get("rows") or [])
    row_count = (sql_result or {}).get("row_count", len(rows))

    preview_rows = []
    for row in rows[:RESULT_PREVIEW_MAX_ROWS]:
        if isinstance(row, dict):
            preview_rows.append(row)
        else:
            preview_rows.append(dict(zip(columns, row)))

    confirmed_plan = state.get("confirmed_plan") or {}
    dimensions = confirmed_plan.get("dimensions") or []
    entity_field = dimensions[0] if dimensions else (columns[0] if columns else "")
    entity_keys = []
    if entity_field:
        seen = set()
        for row in preview_rows:
            key = row.get(entity_field)
            if key is None or key in seen:
                continue
            seen.add(key)
            entity_keys.append(str(key))
            if len(entity_keys) >= RESULT_ENTITY_KEYS_MAX:
                break

    return {
        "result_id": f"{state.get('request_id', '')}:result",
        "source_request_id": state.get("request_id", ""),
        "confirmed_plan": confirmed_plan,
        "columns": columns,
        "preview_rows": preview_rows,
        "row_count": row_count,
        "result_summary": f"共 {row_count} 行，列：{', '.join(columns[:10]) or '无'}",
        "entity_keys": entity_keys,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _build_answer_update(state, final_answer, topic_status, extra_update=None):
    # 将Seeker最终回答同时写入标准Topic消息记忆
    update = {
        "final_answer": final_answer,
        "topic_status": topic_status,
        "messages": [
            AIMessage(
                content=final_answer,
                name="seeker",
                id=f"{state['request_id']}:seeker",
            )
        ],
    }
    if extra_update:
        update.update(extra_update)
    return update


def build_build_final_answer_node(runtime):
    llm = runtime["llm"]

    def build_final_answer_node(state: AgentState):
        # 标记调用方，LLM 日志按业务方归类（结果整理）
        set_llm_caller("build_final_answer")

        sql_valid = state.get("sql_valid", False)
        # 回答基准使用当前有效需求，避免多轮追问后仍按话题首轮原文判断完整性
        question = state.get("effective_query") or ""
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

                update = _build_answer_update(
                    state,
                    f"本次未执行 SQL 查询，因为生成的 SQL 未通过校验。原因：{sql_error}",
                    "failed",
                )
                log_state_snapshot("build_final_answer", {**state, **update})
                return update

            # sql 校验成功
            sql_result = state.get("sql_result", {})
            row_count = sql_result.get("row_count", 0) if isinstance(sql_result, dict) else 0

            # 空结果与成功分支都刷新结果快照，避免旧结果被后续追问错误复用
            snapshot = _build_result_snapshot(state, sql_result)
            result_update = {
                "last_query_result": snapshot,
                "result_id": snapshot["result_id"],
                "result_preview": snapshot["preview_rows"],
            }

            if row_count == 0:

                # 记录空结果分支日志
                log_node_end(
                    "build_final_answer",
                    branch="empty_result",
                    rows=row_count,
                    ms=elapsed_ms(timer),
                )

                update = _build_answer_update(
                    state,
                    "SQL 已成功执行，但没有查询到符合条件的数据。",
                    "completed",
                    extra_update=result_update,
                )
                log_state_snapshot("build_final_answer", {**state, **update})
                return update

            prompt = ChatPromptTemplate.from_messages([
                ("system", FINAL_ANSWER_SYSTEM_PROMPT),
                ("human", FINAL_ANSWER_HUMAN_TEMPLATE),
            ])

            # 补充已确认指标口径，只覆盖当前方案实际包含的指标，
            # 避免历史口径（如退租/净增）被误认为本轮必须回答的字段
            answer_question = question
            plan_measures = set((state.get("confirmed_plan") or {}).get("measures") or [])
            resolved_lines = []
            for resolution in ((state.get("analysis_spec") or {}).get("metric_resolutions") or []):
                if (
                    resolution.get("status") == "resolved"
                    and resolution.get("mention")
                    and resolution.get("selected_field")
                    and (
                        not plan_measures
                        or resolution.get("selected_field") in plan_measures
                    )
                ):
                    resolved_lines.append(
                        f"- {resolution.get('mention')}（字段：{resolution.get('selected_field')}）"
                    )
            if resolved_lines:
                answer_question = (
                    f"{question}\n\n【已确认指标口径，回答时必须全部覆盖】\n"
                    + "\n".join(resolved_lines)
                )
            prompt_value = prompt.invoke({
                "question": answer_question,
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
            update = _build_answer_update(
                state,
                final_answer,
                "completed",
                extra_update=result_update,
            )
            log_state_snapshot("build_final_answer", {**state, **update})
            return update
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
