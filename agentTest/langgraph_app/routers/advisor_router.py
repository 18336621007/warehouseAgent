from agentTest.langgraph_app.state.agent_state import AgentState

def route_after_advisor(state: AgentState):
    if state.get("advisor_confirmed"):
        return "confirm"
    return "clarify"