# SQL 执行节点，负责调用标准 SQL Tool 执行生成的 SQL。
import re

from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState


# ── 独立聚合多表场景的扩展 timeout：默认 300s（与 Hive 手动执行实测对齐） ──
CROSS_JOIN_AGG_TIMEOUT_SECONDS = 300


def _is_cross_join_aggregation(sql: str, confirmed_plan: dict) -> bool:
    """判断 SQL 是否属于'独立聚合多表 CROSS JOIN'场景：
    - SQL 中包含 CROSS JOIN
    - 同时不含字段级 JOIN ON <字段>=<字段>
    - confirmed_plan 标记为 independent_aggregation 或 joins 为空
    """
    if not sql:
        return False
    sql_upper = sql.upper()
    has_cross_join = "CROSS JOIN" in sql_upper
    if not has_cross_join:
        return False
    # 字段级 JOIN ON tbl.a = tbl.b
    field_join_pattern = re.compile(
        r"JOIN\s+\w+(?:\s+\w+)?\s+ON\s+\w+\.\w+\s*=\s*\w+\.\w+",
        re.IGNORECASE,
    )
    if field_join_pattern.search(sql):
        return False
    # confirmed_plan 必须是无 join 的多表场景
    joins = confirmed_plan.get("joins") or []
    tables = confirmed_plan.get("tables") or []
    if joins:
        return False
    return len(tables) >= 2


def build_execute_sql_node(runtime):

    tools = runtime["tools"]
    sql_query_tool = next(tool for tool in tools if tool.name == "sql_query")

    def execute_sql_node(state: AgentState):

        generated_sql = state.get("generated_sql", "")
        timer = start_timer()

        # 记录节点开始日志
        log_node_start("execute_sql", sql=str(generated_sql))

        # 复杂查询：执行前做 Hive EXPLAIN + LIMIT 0 试跑
        confirmed_plan = state.get("confirmed_plan") or {}
        complex_flag = confirmed_plan.get("complex", False)
        if complex_flag and generated_sql:
            try:
                from agentTest.langgraph_app.services.sql_safety_validator import validate_sql_safety
                # Try to get hive_datasource from the tool or runtime
                hive_ds = getattr(sql_query_tool, "_datasource", None)
                safety = validate_sql_safety(generated_sql, hive_datasource=hive_ds)
                if not safety.passed:
                    log_node_error("execute_sql", error=f"Safety check Layer {safety.layer}: {safety.error}", ms=0)
                    return {
                        "sql_exec_failed": True,
                        "sql_exec_error": f"[复杂查询安全校验 Layer {safety.layer}] {safety.error}",
                        "sql_result": None,
                    }
            except Exception as safety_err:
                log_node_event("execute_sql", f"Safety check skipped: {safety_err}")

        try:
            # ── 独立聚合多表 CROSS JOIN 场景：拉长 timeout 到 300s ──
            invoke_kwargs = {"sql": generated_sql}
            if _is_cross_join_aggregation(generated_sql, confirmed_plan):
                log_node_event(
                    "execute_sql",
                    f"检测到独立聚合 CROSS JOIN 场景，使用扩展 timeout={CROSS_JOIN_AGG_TIMEOUT_SECONDS}s",
                )
                # 优先支持工具层 timeout 覆盖；如不支持则在 datasource 层兜底
                if hasattr(sql_query_tool, "query_timeout_seconds"):
                    sql_query_tool.query_timeout_seconds = CROSS_JOIN_AGG_TIMEOUT_SECONDS
                # 部分工具支持在 invoke 时透传 timeout
                try:
                    sql_result = sql_query_tool.invoke(
                        {**invoke_kwargs, "timeout_seconds": CROSS_JOIN_AGG_TIMEOUT_SECONDS}
                    )
                except TypeError:
                    sql_result = sql_query_tool.invoke(invoke_kwargs)
            else:
                sql_result = sql_query_tool.invoke(invoke_kwargs)
            row_count = sql_result.get("row_count", 0) if isinstance(sql_result, dict) else 0

            # 记录节点结束日志
            log_node_end(
                "execute_sql",
                rows=row_count,
                ms=elapsed_ms(timer),
            )

            return {
                "sql_result": sql_result,
                "sql_exec_failed": False,
                "sql_exec_error": "",
            }
        except Exception as error:
            # 记录节点异常日志，不抛异常，交给路由决定重试还是降级
            error_str = str(error)
            log_node_error(
                "execute_sql",
                error=error_str,
                ms=elapsed_ms(timer),
            )
            return {
                "sql_exec_failed": True,
                "sql_exec_error": error_str,
                "sql_result": None,
            }

    return execute_sql_node
