from agentTest.langgraph_app.nodes.build_final_answer_node import build_build_final_answer_node
from agentTest.langgraph_app.nodes.build_schema_context_node import build_schema_context_node
from agentTest.langgraph_app.nodes.execute_sql_node import build_execute_sql_node
from agentTest.langgraph_app.nodes.generate_sql_node import build_generate_sql_node
from agentTest.langgraph_app.nodes.retrieve_schema_node import build_retrieve_schema_node
from agentTest.langgraph_app.nodes.validate_sql_node import validate_sql_node
from agentTest.langgraph_app.state.agent_state import AgentState
from langgraph.graph import StateGraph, START, END

def build_sql_agent_graph(runtime):
    graph = StateGraph(AgentState)

    graph.add_node("retrieve_schema", build_retrieve_schema_node(runtime))
    graph.add_node("build_schema_context", build_schema_context_node)
    graph.add_node("generate_sql", build_generate_sql_node(runtime))
    graph.add_node("validate_sql", validate_sql_node)
    graph.add_node("execute_sql", build_execute_sql_node(runtime))
    graph.add_node("build_final_answer", build_build_final_answer_node(runtime))

    graph.add_edge(START, "retrieve_schema")
    graph.add_edge("retrieve_schema", "build_schema_context")
    graph.add_edge("build_schema_context", "generate_sql")
    graph.add_edge("generate_sql", "validate_sql")
    graph.add_edge("validate_sql", "execute_sql")
    graph.add_edge("execute_sql", "build_final_answer")
    graph.add_edge("build_final_answer", END)

    return graph.compile()



