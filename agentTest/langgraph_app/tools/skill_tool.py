# 技能读取工具：Planner ReAct 里按需读取完整 SKILL.md 指令（渐进式披露，仿 Codex）
from langchain.tools import tool


def build_read_skill_tool(skill_manager):
    """构建技能读取工具：模型根据披露列表判断命中后，调用它读取完整指令正文。"""
    @tool
    def read_skill(skill_name: str) -> str:
        """读取某个技能的完整指令（SKILL.md 正文 + 技能资源目录路径）。

        当当前任务与【可用技能】列表中某个技能的 description 匹配时调用；
        读取后遵循其中规则执行。参数：skill_name 技能名称（见可用技能列表）。
        """
        if skill_manager is None:
            return "技能系统未启用。"
        return skill_manager.load_skill_instruction(skill_name, scope="planner")

    return read_skill
