# 用户消息入口节点，负责将本轮输入写入Topic消息记忆
from langchain_core.messages import HumanMessage

from agentTest.langgraph_app.state.agent_state import AgentState


def capture_user_message_node(state: AgentState):
    request_id = state["request_id"]
    current_user_input = state["current_user_input"]

    return_value = {
        "messages": [
            HumanMessage(
                content=current_user_input,
                name="user",
                # 相同request_id重复执行时，add_messages不会重复追加
                id=f"{request_id}:user",
            )
        ]
    }

    # 去 Topic 化：对话首轮（尚无消息）从 new 状态开始
    if not state.get("messages"):
        return_value["topic_status"] = "new"

    # 用户新输入开启新的查询意图：重置 0 行自愈计数（自愈回环不经本节点，计数得以保留累加）
    return_value["empty_result_rounds"] = 0

    return return_value