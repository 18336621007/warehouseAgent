# 查询方案契约，统一约束 Advisor、Planner 和 Seeker 之间传递的方案结构
from typing import Literal, TypedDict


# draft 表示追问中逐步完善的方案，locked 表示完整方案等待确认，confirmed 表示允许 Seeker 执行
PlanStatus = Literal["draft", "locked", "confirmed"]


class QueryPlan(TypedDict, total=False):
    # table 由 lock_query_plan 从 tables[0] 推导，不作为独立概念暴露给 Advisor
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

    # 聚合后过滤（HAVING 子句），可选
    having: str
    # 排序规则，可选，如 [{"field": "new_order", "direction": "DESC"}]
    order_by: list[dict]
    # 返回行数限制，默认 1000
    result_limit: int
    # 是否为复杂查询（需要窗口函数/子查询/CTE），默认 False
    complex: bool
    # 每表独立子方案，包含表名、时间字段、时间范围、过滤条件
    # 格式: [{"table": "ads.xxx", "time_field": "pt_dt", "time_range": "昨天", "filters": ""}, ...]
    table_plans: list[dict]

    # ── 系统派生的物理执行字段（单表时为空，多表时由 JoinPlanner 填充）──
    joins: list[dict]           # 多表 Join 边，每边包含 left_table/right_table/left_key/right_key/join_type/cardinality
    field_sources: dict         # {字段名: database.table}，标识每个业务字段的物理来源
    target_grain: list[str]     # 查询粒度维度，用于校验 GROUP BY
    metadata_version: str       # 关系元数据版本，用于审计追溯
    # 已解决指标概念到物理字段的映射，未解决候选不允许进入 QueryPlan
    concept_resolutions: dict  # {指标概念: {field, table, source}}

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

    # draft：追问过程中逐步完善的方案，允许槽位为空，只做最小结构校验
    if plan.get("status") == "draft":
        draft_errors = []
        draft_tables = plan.get("tables") or []
        draft_measures = plan.get("measures") or []
        draft_dimensions = plan.get("dimensions") or []
        if not isinstance(draft_measures, list) or any(
            not isinstance(item, str) for item in draft_measures
        ):
            draft_errors.append("measures 必须是字符串列表")
        if not isinstance(draft_dimensions, list) or any(
            not isinstance(item, str) for item in draft_dimensions
        ):
            draft_errors.append("dimensions 必须是字符串列表")
        if (not draft_tables) and (not draft_measures) and (not draft_dimensions):
            draft_errors.append("草稿方案至少需要数据表或字段")
        return draft_errors

    table = plan.get("table", "")
    tables = plan.get("tables") or []
    measures = plan.get("measures") or []
    dimensions = plan.get("dimensions") or []
    fields = plan.get("fields") or []
    time_field = plan.get("time_field", "")
    time_range = plan.get("time_range", "")
    filters = plan.get("filters", "")
    having = plan.get("having", "")
    order_by = plan.get("order_by") or []
    result_limit = plan.get("result_limit", 1000)
    complex_flag = plan.get("complex", False)
    status = plan.get("status", "")
    table_plans = plan.get("table_plans") or []

    # table 由 lock_query_plan 从 tables[0] 推导
    if not table and tables:
        table = tables[0]

    if not isinstance(table, str) or not table.strip():
        errors.append("缺少主表")

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

    # 每张参与表都必须拥有独立时间过滤计划，业务过滤按表配置且不要求一致。
    if not isinstance(table_plans, list):
        errors.append("table_plans 必须是列表")
    else:
        plan_by_table = {
            table_plan.get("table", ""): table_plan
            for table_plan in table_plans
            if isinstance(table_plan, dict) and table_plan.get("table")
        }
        for table_name in tables:
            table_plan = plan_by_table.get(table_name)
            if not table_plan:
                errors.append(f"表 {table_name} 缺少独立过滤计划 table_plan")
                continue

            table_time_field = table_plan.get("time_field", "")
            table_time_range = table_plan.get("time_range", "")
            table_filters = table_plan.get("filters", "")
            if not isinstance(table_time_field, str):
                errors.append(f"表 {table_name} 的 time_field 必须是字符串")
            if not isinstance(table_time_range, str):
                errors.append(f"表 {table_name} 的 time_range 必须是字符串")
            if not isinstance(table_filters, str):
                errors.append(f"表 {table_name} 的 filters 必须是字符串")
            if not str(table_time_field).strip():
                errors.append(f"表 {table_name} 缺少独立时间字段 time_field")
            elif not str(table_time_range).strip():
                errors.append(f"表 {table_name} 设置了 time_field 但缺少 time_range")

    if status not in ("locked", "confirmed"):
        errors.append("status 必须是 locked 或 confirmed")

    # 指标解析证据为可选审计字段，存在时必须是字典
    concept_resolutions = plan.get("concept_resolutions")
    if concept_resolutions is not None and not isinstance(concept_resolutions, dict):
        errors.append("concept_resolutions 必须是字典")

    if require_confirmed and status != "confirmed":
        errors.append("查询方案尚未经过用户最终确认")

    return errors
