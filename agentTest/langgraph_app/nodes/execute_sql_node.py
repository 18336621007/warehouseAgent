# SQL 执行节点，负责调用标准 SQL Tool 执行生成的 SQL。
import re

from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.tools.sql_query_tool import QueryCancelledError
from agentTest.langgraph_app.services.sql_table_filter_validator import resolve_required_filter_fields


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

    tool_registry = runtime["tool_registry"]

    def _get_sql_tool(engine):
        # 按引擎名取 SQL 工具；未注册返回 None（降级链自动跳过该引擎）
        try:
            return tool_registry.get_by_name(f"sql_query_{engine}").tool
        except Exception:
            return None

    def execute_sql_node(state: AgentState):

        generated_sql = state.get("generated_sql", "")
        timer = start_timer()

        # 记录节点开始日志
        log_node_start("execute_sql", sql=str(generated_sql))

        # 引擎候选链：语义层方案决定（data_project -> doris，其余 -> trino 优先/hive 兜底）
        confirmed_plan = state.get("confirmed_plan") or {}
        engine_candidates = confirmed_plan.get("engine_candidates") or ["trino", "hive"]
        # 跨数据源关联：第一期拒绝，明确提示拆开查询
        if confirmed_plan.get("cross_engine"):
            log_node_error("execute_sql", error="跨数据源关联暂不支持", ms=0)
            return {
                "sql_exec_failed": True,
                "sql_exec_error": "查询涉及跨数据源关联，暂不支持一次查询多引擎表，请拆开分别查询",
                "sql_result": None,
            }

        complex_flag = confirmed_plan.get("complex", False)
        # 逐候选引擎执行：连接/执行异常时降级下一个候选；校验失败（ValueError）不降级
        errors = []
        for engine in engine_candidates:
            sql_query_tool = _get_sql_tool(engine)
            if sql_query_tool is None:
                errors.append(f"[{engine}] 引擎未注册")
                continue
            log_node_event("execute_sql", f"尝试引擎 {engine} 执行查询")

            # 复杂查询：执行前做安全预检（引擎无关，预检弱依赖失败时跳过）
            if complex_flag and generated_sql:
                try:
                    from agentTest.langgraph_app.services.sql_safety_validator import validate_sql_safety
                    # Try to get datasource from the tool or runtime
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
                # 明细查询（无 pt_dt 分区）按方案时间字段透传给执行守卫，
                # 与 validate_sql_node 保持一致，避免无 pt_dt 的明细表被误判为全表扫描。
                # 扩展：聚合查询同样按表实际分区字段透传，无 pt_dt 分区时用方案业务时间字段。
                try:
                    partition_fields = resolve_required_filter_fields(confirmed_plan)
                except Exception:
                    # 语义层不可用时回退默认分区字段，不阻断执行
                    partition_fields = ["pt_dt"]
                invoke_kwargs["partition_fields"] = partition_fields
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

                # 记录节点结束日志（含引擎标识，便于审计实际执行引擎）
                log_node_end(
                    "execute_sql",
                    rows=row_count,
                    ms=elapsed_ms(timer),
                    engine=engine,
                )
                return {
                    "sql_result": sql_result,
                    "sql_exec_failed": False,
                    "sql_exec_error": "",
                }
            except QueryCancelledError:
                # 用户在 SQL 执行期间点了停止：不再降级，直接返回取消标记
                log_node_error("execute_sql", error="查询已停止（用户终止本轮生成）", ms=elapsed_ms(timer))
                return {
                    "sql_exec_failed": True,
                    "sql_exec_error": "查询已停止",
                    "sql_result": None,
                }
            except ValueError as error:
                # 校验失败：引擎无关，不降级，直接返回（换引擎大概率同样拒绝）
                log_node_error("execute_sql", error=f"[{engine}] {error}", ms=elapsed_ms(timer))
                return {
                    "sql_exec_failed": True,
                    "sql_exec_error": str(error),
                    "sql_result": None,
                }
            except Exception as error:
                # 执行异常：降级到下一个候选引擎，全部失败返回汇总错误
                error_str = f"[{engine}] {error}"
                errors.append(error_str)
                log_node_error("execute_sql", error=error_str, ms=elapsed_ms(timer))
                continue

        # 所有候选引擎均失败：汇总各引擎错误返回
        return {
            "sql_exec_failed": True,
            "sql_exec_error": "；".join(errors) or "无可用查询引擎",
            "sql_result": None,
        }

    return execute_sql_node
