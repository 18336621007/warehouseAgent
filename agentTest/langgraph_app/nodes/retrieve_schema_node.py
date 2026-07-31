# Schema 精确加载节点，只根据最终确认方案读取物理表结构。
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState


def build_retrieve_schema_node(runtime):
    query_plan_schema_resolver = (
        runtime["query_plan_schema_resolver"]
    )

    def retrieve_schema_node(
        state: AgentState,
    ) -> dict:
        timer = start_timer()
        confirmed_plan = (
            state.get("confirmed_plan") or {}
        )

        # 记录最终确认方案的 Schema 加载过程
        log_node_start(
            "retrieve_schema",
            plan_status=confirmed_plan.get(
                "status",
                "",
            ),
        )

        try:
            resolved_schema = (
                query_plan_schema_resolver.resolve(
                    confirmed_plan
                )
            )

            log_node_end(
                "retrieve_schema",
                table=resolved_schema[
                    "table_identifier"
                ],
                fields=resolved_schema[
                    "column_count"
                ],
                ms=elapsed_ms(timer),
            )

            return {
                "schema_context": resolved_schema[
                    "schema_context"
                ],
                # Schema 已准备完成，下一阶段开始生成 SQL
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