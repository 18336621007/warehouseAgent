# Schema 精确加载节点，根据最终确认方案读取物理表结构
# 集成字段覆盖分析 + Join 路径规划，单表直接解析，多表尝试安全 Join
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms, log_node_event
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.services.table_coverage_analyzer import (
    TableCoverageAnalyzer,
)
from agentTest.langgraph_app.services.join_planner import JoinPlanner


def build_retrieve_schema_node(runtime):
    query_plan_schema_resolver = (
        runtime["query_plan_schema_resolver"]
    )
    column_vector_store = runtime["column_vector_store"]
    # 字段归属判定必须使用 SemanticMetadataProvider（含 SemanticLayerProvider 权威 partition/fields），
    # 不应使用 HiveMetadataProvider（仅返回 Hive 元数据，没有 partition 字段语义）
    semantic_provider = runtime["semantic_metadata_provider"]

    coverage_analyzer = TableCoverageAnalyzer(
        column_vector_store,
        semantic_provider,
    )
    join_planner = JoinPlanner(semantic_provider)

    def retrieve_schema_node(
        state: AgentState,
    ) -> dict:
        timer = start_timer()
        confirmed_plan = (
            state.get("confirmed_plan") or {}
        )

        log_node_start(
            "retrieve_schema",
            plan_status=confirmed_plan.get("status", ""),
        )

        try:
            # ---- 步骤一：字段覆盖分析 ----
            coverage = coverage_analyzer.analyze(confirmed_plan)

            if coverage.uncovered_fields:
                raise ValueError(
                    "以下字段无法映射到物理表，请确认字段名是否正确："
                    + ", ".join(coverage.uncovered_fields)
                )

            log_node_event(
                "retrieve_schema",
                f"覆盖分析: 单表={coverage.single_table}, "
                f"涉及表={coverage.needed_tables}, "
                f"字段来源={coverage.field_sources}",
            )

            # ---- 步骤二：多表 Join 路径规划 ----
            if not coverage.single_table:
                join_result = join_planner.plan(
                    coverage.needed_tables,
                    coverage.field_sources,
                )

                if not join_result.success:
                    # 安全拒绝：缺少关系配置且不允许 AI 推断
                    error_msg = (
                        "当前查询涉及多张表，但缺少必要的关联关系配置，"
                        "无法安全执行 Join：\n"
                        + "\n".join(
                            f"  - {rel}"
                            for rel in join_result.missing_relations
                        )
                        + "\n请联系数据管理员补充语义层 join_contracts 中的表关系配置。"
                    )
                    log_node_error("retrieve_schema", error=error_msg, ms=elapsed_ms(timer))
                    raise ValueError(error_msg)

                # Join 路径已找到（或允许 AI 推断），更新 confirmed_plan
                confirmed_plan["joins"] = join_result.join_edges
                confirmed_plan["field_sources"] = join_result.field_sources
                confirmed_plan["target_grain"] = join_result.target_grain
                confirmed_plan["tables"] = coverage.needed_tables
                confirmed_plan["table"] = coverage.needed_tables[0]

                if join_result.needs_ai_inference:
                    confirmed_plan["ai_inferred_join"] = True
                    log_node_event(
                        "retrieve_schema",
                        f"AI 推断 Join 模式: edges={len(join_result.join_edges)}, "
                        f"missing_relations={join_result.missing_relations}",
                    )
                # Join 路径已找到，更新 confirmed_plan
                confirmed_plan["joins"] = join_result.join_edges
                confirmed_plan["field_sources"] = join_result.field_sources
                confirmed_plan["target_grain"] = join_result.target_grain
                confirmed_plan["tables"] = coverage.needed_tables
                confirmed_plan["table"] = coverage.needed_tables[0]

                log_node_event(
                    "retrieve_schema",
                    f"Join 路径已规划: edges={len(join_result.join_edges)}, "
                    f"grain={join_result.target_grain}",
                )

            # ---- 步骤三：单表 Schema 解析 ----
            resolved_schema = (
                query_plan_schema_resolver.resolve(
                    confirmed_plan
                )
            )

            log_node_end(
                "retrieve_schema",
                table=resolved_schema["table_identifier"],
                fields=resolved_schema["column_count"],
                ms=elapsed_ms(timer),
            )

            return {
                "schema_context": resolved_schema["schema_context"],
                "topic_status": "generating_sql",
            }
        except Exception as error:
            log_node_error(
                "retrieve_schema",
                error=str(error),
                ms=elapsed_ms(timer),
            )
            raise

    return retrieve_schema_node