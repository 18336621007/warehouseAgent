# Advisor 确认工具：用户明确选择口径后，Advisor 调用此工具锁定最终方案。
# 工具本身只返回确认摘要，真正的 state 写入在 advisor_graph.py 的 run_advisor 中处理。
# 重构：拆分为 table + measures + dimensions + time_field + filters，结构性强约束
from langchain_core.tools import tool


@tool
def confirm_selection(
    table: str,
    measures: list[str],
    dimensions: list[str],
    time_field: str = "pt_dt",
    filters: str = "",
) -> str:
    """当用户明确确认了某个分析方案后调用。

    参数说明：
    - table: 完整表名，如 'ads_trip.ads_exchange_platform_operations_report_day'
    - measures: 度量字段列表，如 ['pay_order_num']，至少一个
    - dimensions: 维度字段列表，如 ['region_name', 'pt_platform']，无则为 []
    - time_field: 时间分区字段，默认 'pt_dt'
    - filters: 额外过滤条件（SQL WHERE 片段），如 "rent_detail_status = '支付成功'"，无则为空字符串

    调用时机（由 Advisor prompt 中的「确认规则」指导）：
    - 用户说序号（'1'/'3'）、关键词确认某个选项
    - 用户说'好的''就这个''按你说的来'等表示同意推荐方案

    不要调用的时机：
    - 用户还在犹豫、比较选项
    - 用户说'都不是''换一个''不对'
    - 你还没向用户列出过选项

    Returns:
        确认摘要字符串
    """
    fields = measures + dimensions + [time_field]
    return f"已确认: 表={table}, 度量={measures}, 维度={dimensions}, 时间={time_field}, 过滤={filters or '无'}"
