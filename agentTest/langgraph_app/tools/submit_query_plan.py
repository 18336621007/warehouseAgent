# Advisor 方案提交工具：将完整查询方案提交给程序校验并锁定
# 支持单表和多表，field_sources 使用 "db.table.field" 完整标识避免同名字段歧义
from langchain_core.tools import tool


@tool
def submit_query_plan(
    tables: list[str],
    measures: list[str] = None,
    dimensions: list[str] = None,
    time_field: str = "pt_dt",
    time_range: str = "",
    filters: str = "",
    field_sources: list[str] = None,   # ["db.table.field", ...]
    order_by: list[dict] = None,       # [{"field": "new_order", "direction": "DESC"}]
    having: str = "",                  # 聚合后过滤条件，如 "SUM(new_order) > 1000"
    result_limit: int = 1000,          # 返回行数限制
    complex: bool = False,             # Planner 判定的复杂查询标记（窗口函数/子查询/CTE）
    concept_resolutions: list[dict] = None,  # 指标解析证据（审计用，程序校验字段合法性后信任）
) -> str:
    """当当前需求已经能够形成完整、唯一的查询方案时调用。

    该工具仅提交方案并等待用户最终确认，不会执行查询。

    参数说明：
    - tables: 查询涉及的全部表列表，单表如 ["ads_trip.xxx"]，多表如 ["ads_trip.xxx", "dim_trip.yyy"]
    - measures: 度量字段列表（裸字段名）。纯维度查询时传空列表 []
    - dimensions: 维度字段列表（裸字段名）。无维度时传空列表 []
    - time_field: 目标表中的时间字段
    - time_range: 用户确认的时间范围，如 昨天、最近7天
    - filters: 额外过滤条件，没有时传空字符串 ""
    - field_sources: 每个字段的完整物理标识列表，格式 ["db.table.field", ...]
      字段跨多表时必须传入。示例：["ads_trip.report.new_order", "dim_trip.company.true_name"]
      仅当所有字段都在同一张表时可省略

    调用要求：
    - 必须先对所有目标表调用 search_columns 核对字段
    - 存在多个未确定口径时不能调用
    - 字段跨多表时必须传 field_sources 标注每个字段的物理来源
    - concept_resolutions 为可审计解析证据，字段必须来自候选列表或上轮已确认字段，程序校验后信任
    """
    return (
        f"方案已提交: 表={tables}, 度量={measures}, "
        f"维度={dimensions}, 时间={time_field}({time_range or '未指定'}), "
        f"过滤={filters or '无'}, field_sources={len(field_sources or [])}条, "
        f"concept_resolutions={len(concept_resolutions or [])}条"
    )
