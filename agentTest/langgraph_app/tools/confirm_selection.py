# Advisor 确认工具：用户明确选择口径后，Advisor 调用此工具锁定最终方案。
# 工具本身只返回确认摘要，真正的 state 写入在 advisor_graph.py 的 run_advisor 中处理。
# 参数结构：table + measures + dimensions + time_field + time_range + filters
from langchain_core.tools import tool


@tool
def confirm_selection(
    table: str,
    measures: list[str],
    dimensions: list[str],
    time_field: str = "pt_dt",
    time_range: str = "",
    filters: str = "",
) -> str:
    """当用户明确确认了某个分析方案后调用。

    参数说明：
    - table: 完整表名，如 'ads_trip.ads_exchange_platform_operations_report_day'
    - measures: 度量字段列表，如 ['pay_order_num']，至少一个。纯查询场景（如"查经理"）可为空 []
    - dimensions: 维度字段列表，如 ['region_name', 'pt_platform']，无则为 []
    - time_field: 时间分区字段，默认 'pt_dt'
    - time_range: 时间范围描述，如 "昨天"、"今天"、"最近7天"、"2026-07-20"。用于 SQL 校验，
      确保生成的 SQL 使用正确的日期函数（昨天→date_sub, 今天→current_date）。
      用户未指定时间则为空字符串 ""。
    - filters: 额外过滤条件（SQL WHERE 片段），如 "region_name = '北京大区'"，无则为空字符串

    调用时机（由 Advisor prompt 中的确认规则指导）：
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
    return f"已确认: 表={table}, 度量={measures}, 维度={dimensions}, 时间={time_field}({time_range or '未指定'}), 过滤={filters or '无'}"
