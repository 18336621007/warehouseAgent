from agentTest.langgraph_app.state.agent_state import AgentState


def route_after_planner(state: AgentState):
    return state.get("route", "advidsor")