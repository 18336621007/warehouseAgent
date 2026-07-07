#会话管理
class Conversation:
    """
    管理整个会话
    """
    def __init__(self):
        self.message = []

    def add_message(self, role, content):
        self.message.append(
            {
                "role":role,
                "content":content
            }
        )
    def add_user(self, content):
        self.add_message(
            role="user",
            content=content
        )

    def add_assistant(self, content):
        self.add_message(
            role="assistant",
            content=content
        )

    def get_messages(self):
        return self.message