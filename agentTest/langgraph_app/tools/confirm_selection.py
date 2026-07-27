# Advisor 确认工具：用户明确选择口径后，Advisor 调用此工具锁定最终方案。
# 工具本身只返回确认摘要，真正的 state 写入在 advisor_graph.py 的 run_advisor 中处理。
# 为什么不在工具内写 state？因为 @tool 是无状态的，不知道 LangGraph state 的存在。
from langchain_core.tools import tool


@tool
def confirm_selection(table: str, fields: list[str]) -> str:
    """当用户明确确认了某个分析方案（表+字段组合）后调用。

    调用时机（由 Advisor prompt 中的「确认规则」指导）：
    - 用户说"1"/"A"/"第一个" 且能回溯到上一轮列出的选项
    - 用户说"好的""就这个""按你说的来" 等表示同意推荐方案
    - 用户用自然语言明确指定的字段已经存在于你列出的选项中

    不要调用的时机：
    - 用户还在犹豫、比较选项
    - 用户说"都不是""换一个""不对"
    - 你还没向用户列出过选项

    Args:
        table: 完整表名，例如 'ads_trip.ads_exchange_platform_operations_report_day'
        fields: 确认的字段列表，至少包含一个度量字段，维度字段（如 pt_platform）如有也加入，
                例如 ['reflow_addition_order', 'pt_platform']
    """
    return f"已确认: 表={table}, 字段={fields}"
