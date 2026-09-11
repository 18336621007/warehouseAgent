# 执行链收尾节点：结果强制落盘 + 回 Planner 评审
# A1：build_final_answer 删除后，执行成功的查询统一在此落盘。
#   - 有数据 → execution_review=True，回 Planner 基于结果撰写最终回答（respond）；
#   - 0 行   → seeker_empty_result=True，回 Planner 用 probe_values 自愈或确认无数据后告知。
# 落盘是旁路能力：save_query_result 内部已吞掉写盘失败，不影响主流程。
import datetime

from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.services.result_store import save_query_result
from agentTest.config.planner import MAX_EMPTY_RESULT_ROUNDS


# 结果快照只保存预览与引用，避免把全量结果写入 checkpoint
RESULT_PREVIEW_MAX_ROWS = 20
RESULT_ENTITY_KEYS_MAX = 50


def _build_result_snapshot(state, sql_result, stored):
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

    snapshot = {
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
    if stored.get("round_no"):
        snapshot["round_no"] = stored.get("round_no")
        snapshot["result_file"] = stored.get("result_file")
        snapshot["full_csv"] = stored.get("full_csv")
    return snapshot


def persist_result_node(state: AgentState):
    """执行链收尾：落盘 + 回 Planner。有数据回看写回答，0 行回 Planner 自愈。"""
    timer = start_timer()
    log_node_start("persist_result")

    try:
        sql_result = state.get("sql_result") or {}
        row_count = sql_result.get("row_count", 0) if isinstance(sql_result, dict) else 0
        # 结果强制落盘（save_query_result 内部吞掉写盘失败，不阻断主流程）
        stored = save_query_result(state, sql_result)
        snapshot = _build_result_snapshot(state, sql_result, stored)
        result_update = {
            "last_query_result": snapshot,
            "result_id": snapshot["result_id"],
            "result_preview": snapshot["preview_rows"],
            "result_csv": snapshot.get("full_csv", ""),
            "plan_results": [snapshot],
        }

        if row_count == 0:
            # 0 行：回 Planner 核实（过滤值不匹配或确实无数据），未达上限自愈，达上限如实告知
            empty_rounds = state.get("empty_result_rounds") or 0
            if empty_rounds < MAX_EMPTY_RESULT_ROUNDS:
                update = {
                    "seeker_empty_result": True,
                    "empty_result_rounds": empty_rounds + 1,
                    "execution_review": False,
                    "evaluator_pending": False,
                    "topic_status": "generating_sql",
                    "self_heal_note": "执行返回 0 行，需核实是过滤条件与实际存储值不一致，还是确实无匹配数据，返回核实后决定重查或直接告知。",
                    **result_update,
                }
                log_node_end("persist_result", branch="empty_self_heal", rows=0, rounds=empty_rounds, ms=elapsed_ms(timer))
                log_state_snapshot("persist_result", {**state, **update})
                return update

            # 已达 0 行重试上限：仍回 Planner，由 Planner 依据上限提示直接告知用户无数据
            update = {
                "seeker_empty_result": True,
                "empty_result_rounds": empty_rounds + 1,
                "execution_review": False,
                "evaluator_pending": False,
                "topic_status": "generating_sql",
                "self_heal_note": "0 行已达重试上限，回 Planner 确认无数据后直接告知用户。",
                **result_update,
            }
            log_node_end("persist_result", branch="empty_result_limit", rows=0, rounds=empty_rounds, ms=elapsed_ms(timer))
            log_state_snapshot("persist_result", {**state, **update})
            return update

        # 有数据：回 Planner 评审撰写最终回答，并触发 Evaluator 评估本轮问答质量
        update = {
            "execution_review": True,
            "evaluator_pending": True,
            "seeker_empty_result": False,
            "topic_status": "executing",
            **result_update,
        }
        log_node_end("persist_result", branch="review", rows=row_count, ms=elapsed_ms(timer))
        log_state_snapshot("persist_result", {**state, **update})
        return update
    except Exception as error:
        # 落盘/快照异常不应阻断：记录后回 Planner 用内存结果评审
        log_node_error("persist_result", error=str(error), ms=elapsed_ms(timer))
        raise
