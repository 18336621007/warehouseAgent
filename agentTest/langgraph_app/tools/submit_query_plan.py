# Advisor 方案提交工具：将完整查询方案提交给程序校验并锁定
from langchain_core.tools import tool


@tool
def submit_query_plan(
    table: str,
    measures: list[str],
    dimensions: list[str],
    time_field: str = "pt_dt",
    time_range: str = "",
    filters: str = "",
) -> str:
    """当当前需求已经能够形成完整、唯一的查询方案时调用。

    该工具仅提交方案并等待用户最终确认，不会执行查询。

    参数说明：
    - table: 完整表名，如 ads_trip.ads_exchange_platform_operations_report_day
    - measures: 度量字段列表，纯维度查询允许为空列表
    - dimensions: 维度字段列表，无维度时为空列表
    - time_field: 时间字段，如 pt_dt
    - time_range: 用户确认的时间范围，如 昨天、最近7天
    - filters: 额外过滤条件，没有时为空字符串

    调用要求：
    - 方案中的表、度量、维度、时间和过滤条件必须完整
    - 必须先在目标表内调用 search_columns
    - 存在多个未确定口径时不能调用
    - 用户局部修改方案时，必须提交修改后的完整方案
    - 该工具不是用户最终执行确认，最终确认由 Planner 处理
    """
    return (
        f"方案已提交: 表={table}, 度量={measures}, "
        f"维度={dimensions}, 时间={time_field}({time_range or '未指定'}), "
        f"过滤={filters or '无'}"
    )