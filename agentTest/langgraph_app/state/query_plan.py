# 查询方案契约，统一约束 Advisor、Planner 和 Seeker 之间传递的方案结构
from typing import Literal, TypedDict


# locked 表示完整方案等待确认，confirmed 表示允许 Seeker 执行
PlanStatus = Literal["locked", "confirmed"]


class QueryPlan(TypedDict, total=False):
    # 当前阶段只支持单表，table 作为主表，tables 为后续多表查询预留
    table: str
    tables: list[str]

    # 用户确认的度量、维度和全部查询字段
    measures: list[str]
    dimensions: list[str]
    fields: list[str]

    # 时间范围和额外过滤条件
    time_field: str
    time_range: str
    filters: str

    # ── 系统派生的物理执行字段（单表时为空，多表时由 JoinPlanner 填充）──
    joins: list[dict]           # 多表 Join 边，每边包含 left_table/right_table/left_key/right_key/join_type/cardinality
    field_sources: dict         # {字段名: database.table}，标识每个业务字段的物理来源
    target_grain: list[str]     # 查询粒度维度，用于校验 GROUP BY
    metadata_version: str       # 关系元数据版本，用于审计追溯

    # 方案确认状态和时间
    status: PlanStatus
    locked_at: str
    confirmed_at: str


def validate_query_plan(
    plan: dict,
    require_confirmed: bool = False,
) -> list[str]:
    """校验查询方案结构，返回所有错误原因。"""
    errors = []

    if not isinstance(plan, dict):
        return ["查询方案必须是字典"]

    table = plan.get("table", "")
    tables = plan.get("tables") or []
    measures = plan.get("measures") or []
    dimensions = plan.get("dimensions") or []
    fields = plan.get("fields") or []
    time_field = plan.get("time_field", "")
    time_range = plan.get("time_range", "")
    filters = plan.get("filters", "")
    status = plan.get("status", "")

    # 当前单表阶段要求 table 和 tables 同时存在
    if not isinstance(table, str) or not table.strip():
        errors.append("缺少主表 table")

    if not isinstance(tables, list) or not tables:
        errors.append("缺少数据表列表 tables")
    elif table and table not in tables:
        errors.append("主表 table 不在 tables 中")

    # 列表字段必须保持统一类型
    list_fields = {
        "measures": measures,
        "dimensions": dimensions,
        "fields": fields,
    }
    for field_name, field_values in list_fields.items():
        if not isinstance(field_values, list):
            errors.append(f"{field_name} 必须是列表")
            continue

        if any(
            not isinstance(field_value, str) or not field_value.strip()
            for field_value in field_values
        ):
            errors.append(f"{field_name} 中存在非法字段名")

    # 纯维度查询允许 measures 为空，但不能连维度也没有
    if not measures and not dimensions:
        errors.append("measures 和 dimensions 不能同时为空")

    if not isinstance(time_field, str) or not time_field.strip():
        errors.append("缺少时间字段 time_field")

    if not isinstance(time_range, str):
        errors.append("time_range 必须是字符串")

    if not isinstance(filters, str):
        errors.append("filters 必须是字符串")

    # fields 必须覆盖所有度量、维度和时间字段
    required_fields = list(measures) + list(dimensions)
    if time_field:
        required_fields.append(time_field)

    missing_fields = [
        field_name
        for field_name in required_fields
        if field_name not in fields
    ]
    if missing_fields:
        errors.append(
            "fields 缺少已确认字段: "
            + ", ".join(missing_fields)
        )

    if status not in ("locked", "confirmed"):
        errors.append("status 必须是 locked 或 confirmed")

    if require_confirmed and status != "confirmed":
        errors.append("查询方案尚未经过用户最终确认")

    return errors
