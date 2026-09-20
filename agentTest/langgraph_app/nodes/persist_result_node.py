# 执行链收尾节点：结果强制落盘（供 execute_query 工具回填给 Agent 撰写回答）
#   - 有数据 → 结果快照回填，Agent 基于预览/全量 CSV 直接写回答；
#   - 0 行   → seeker_empty_result=True，回 Agent 用 probe_values 自愈或确认无数据后告知。
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
# 结果行数不超过该值时全量预览（小结果直接给全，LLM 基于完整查询结果总结更准确）
RESULT_PREVIEW_FULL_THRESHOLD = 100
RESULT_ENTITY_KEYS_MAX = 50


def _build_result_snapshot(state, sql_result, stored):
    """查询成功后生成结构化结果快照（QueryResultSnapshot）：引用+预览+实体键。"""
    columns = list((sql_result or {}).get("columns") or [])
    rows = list((sql_result or {}).get("rows") or [])
    row_count = (sql_result or {}).get("row_count", len(rows))

    # 小结果全量预览供 LLM 总结，避免预览截断导致统计不完整；大结果取前 N 行防 token 膨胀
    _preview_limit = RESULT_PREVIEW_MAX_ROWS if len(rows) > RESULT_PREVIEW_FULL_THRESHOLD else len(rows)
    preview_rows = []
    for row in rows[:_preview_limit]:
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
        }

        if row_count == 0:
            # 0 行：回 Agent 核实（过滤值不匹配或确实无数据），未达上限自愈，达上限如实告知
            empty_rounds = state.get("empty_result_rounds") or 0
            if empty_rounds < MAX_EMPTY_RESULT_ROUNDS:
                update = {
                    "seeker_empty_result": True,
                    "empty_result_rounds": empty_rounds + 1,
                    "topic_status": "generating_sql",
                    "self_heal_note": "执行返回 0 行，需核实是过滤条件与实际存储值不一致，还是确实无匹配数据，返回核实后决定重查或直接告知。",
                    **result_update,
                }
                log_node_end("persist_result", branch="empty_self_heal", rows=0, rounds=empty_rounds, ms=elapsed_ms(timer))
                log_state_snapshot("persist_result", {**state, **update})
                return update

            # 已达 0 行重试上限：仍回 Agent，由 Agent 依据上限提示直接告知用户无数据
            update = {
                "seeker_empty_result": True,
                "empty_result_rounds": empty_rounds + 1,
                "topic_status": "generating_sql",
                "self_heal_note": "0 行已达重试上限，回 Agent 确认无数据后直接告知用户。",
                **result_update,
            }
            log_node_end("persist_result", branch="empty_result_limit", rows=0, rounds=empty_rounds, ms=elapsed_ms(timer))
            log_state_snapshot("persist_result", {**state, **update})
            return update

        # 有数据：结果快照回填，供 Agent 基于预览/全量 CSV 撰写最终回答
        update = {
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
