# 用户消息入口节点，负责将本轮输入写入Topic消息记忆
from langchain_core.messages import HumanMessage

from agentTest.langgraph_app.state.agent_state import AgentState


def capture_user_message_node(state: AgentState):
    request_id = state["request_id"]
    current_user_input = state["current_user_input"]

    return {
        "messages": [
            HumanMessage(
                content=current_user_input,
                name="user",
                # 相同request_id重复执行时，add_messages不会重复追加
                id=f"{request_id}:user",
            )
        ]
    }