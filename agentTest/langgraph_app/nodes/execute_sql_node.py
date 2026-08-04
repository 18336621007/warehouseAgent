# SQL 执行节点，负责调用标准 SQL Tool 执行生成的 SQL。
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState


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
            sql_result = sql_query_tool.invoke({
                "sql": generated_sql
            })
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
