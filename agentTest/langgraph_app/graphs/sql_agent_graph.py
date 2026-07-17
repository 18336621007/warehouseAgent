from agentTest.langgraph_app.nodes.build_final_answer_node import build_final_answer_node
from agentTest.langgraph_app.nodes.build_schema_context_node import build_schema_context_node
from agentTest.langgraph_app.nodes.execute_sql_node import execute_sql_node
from agentTest.langgraph_app.nodes.generate_sql_node import generate_sql_node
from agentTest.langgraph_app.nodes.retrieve_schema_node import retrieve_schema_node
from agentTest.langgraph_app.state.agent_state import AgentState
from langgraph.graph import StateGraph, START, END

def build_sql_agent_graph():
    graph = StateGraph(AgentState)

    graph.add_node("retrieve_schema", retrieve_schema_node)
    graph.add_node("build_schema_context", build_schema_context_node)
    graph.add_node("generate_sql", generate_sql_node)
    graph.add_node("execute_sql", execute_sql_node)
    graph.add_node("build_final_answer", build_final_answer_node)

    graph.add_edge(START, "retrieve_schema")
    graph.add_edge("retrieve_schema", "build_schema_context")
    graph.add_edge("build_schema_context", "generate_sql")
    graph.add_edge("generate_sql", "execute_sql")
    graph.add_edge("execute_sql", "build_final_answer")
    graph.add_edge("build_final_answer", END)

    return graph.compile()



