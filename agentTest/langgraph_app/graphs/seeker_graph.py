from agentTest.langgraph_app.nodes.build_final_answer_node import build_build_final_answer_node
from agentTest.langgraph_app.nodes.execute_sql_node import build_execute_sql_node
from agentTest.langgraph_app.nodes.generate_sql_node import build_generate_sql_node
from agentTest.langgraph_app.nodes.retrieve_schema_node import build_retrieve_schema_node
from agentTest.langgraph_app.nodes.validate_sql_node import validate_sql_node
from agentTest.langgraph_app.nodes.evaluator_node import build_evaluator_node
from agentTest.langgraph_app.routers.sql_router import route_after_sql_validation
from agentTest.langgraph_app.routers.sql_exec_router import route_after_sql_execution
from agentTest.langgraph_app.routers.seeker_router import route_after_schema
from agentTest.langgraph_app.state.seeker_state import SeekerState
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.nodes.prepare_sql_fix_node import prepare_sql_fix_node
from agentTest.langgraph_app.nodes.prepare_sql_exec_fix_node import prepare_sql_exec_fix_node

# 构建 Seeker 子图：处理明确需求
# 作为 Supervisor 父图的子图使用，不再自闭环（START/END 由父图对接）
def build_seeker_subgraph(runtime):
    graph = StateGraph(SeekerState)

    graph.add_node("retrieve_schema", build_retrieve_schema_node(runtime))
    graph.add_node("generate_sql", build_generate_sql_node(runtime))
    graph.add_node("validate_sql", validate_sql_node)
    graph.add_node("prepare_sql_fix", prepare_sql_fix_node)
    graph.add_node("execute_sql", build_execute_sql_node(runtime))
    graph.add_node("prepare_sql_exec_fix", prepare_sql_exec_fix_node)
    graph.add_node("build_final_answer", build_build_final_answer_node(runtime))
    graph.add_node("evaluator", build_evaluator_node(runtime))

    # 方案不可行时结束子图（无操作），失败原因已写入 state，由父图处理
    graph.add_node("end_plan_error", lambda state: {})

    graph.add_edge(START, "retrieve_schema")

    # Schema 已由最终确认方案精确加载，可以直接生成 SQL
    # 方案不可行（缺 join 契约/字段无归属）时短路到 END，交由父图回 Planner 修复
    graph.add_conditional_edges(
        "retrieve_schema",
        route_after_schema,
        {
            "generate": "generate_sql",
            "plan_error": "end_plan_error",
        },
    )
    graph.add_edge("end_plan_error", END)
    graph.add_edge("generate_sql", "validate_sql")


    graph.add_conditional_edges(
        "validate_sql",
        route_after_sql_validation,
        {
            "execute": "execute_sql",
            "fix": "prepare_sql_fix",
            "end": "build_final_answer",
        }
    )

    graph.add_edge("prepare_sql_fix", "generate_sql")

    # SQL 执行失败重试回路：成功→final_answer，失败→fix→generate_sql，耗尽→降级→final_answer
    graph.add_conditional_edges(
        "execute_sql",
        route_after_sql_execution,
        {
            "success": "build_final_answer",
            "retry": "prepare_sql_exec_fix",
            "degrade": "build_final_answer",
        }
    )
    graph.add_edge("prepare_sql_exec_fix", "generate_sql")

    graph.add_edge("build_final_answer", "evaluator")
    graph.add_edge("evaluator", END)

    return graph.compile()
