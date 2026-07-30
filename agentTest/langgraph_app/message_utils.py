# Topic消息读取工具，统一处理用户、Advisor和Seeker消息
from langchain_core.messages import AIMessage, HumanMessage


def get_last_ai_content(messages, message_name):
    # 从后向前查找指定Agent最近一次可见回复
    for message in reversed(messages or []):
        if (
            isinstance(message, AIMessage)
            and getattr(message, "name", None) == message_name
            and message.content
        ):
            return str(message.content)

    return ""


def build_advisor_dialogue_context(messages, limit=5):
    # Evaluator只读取用户和Advisor之间的可见对话
    dialogue_lines = []

    for message in messages or []:
        message_name = getattr(message, "name", None)
        content = getattr(message, "content", "")

        if not content:
            continue

        if isinstance(message, HumanMessage) and message_name == "user":
            dialogue_lines.append(f"用户：{str(content)[:300]}")
        elif isinstance(message, AIMessage) and message_name == "advisor":
            dialogue_lines.append(f"Advisor：{str(content)[:300]}")

    return " | ".join(dialogue_lines[-limit:])
