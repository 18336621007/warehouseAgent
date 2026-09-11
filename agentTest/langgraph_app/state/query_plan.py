# 查询方案契约，统一约束 Advisor、Planner 和 Seeker 之间传递的方案结构
from typing import Literal, TypedDict


# 方案不再区分草稿/提交：共享方案统一为 confirmed，是否完整由 Planner 判定
PlanStatus = Literal["confirmed"]


class QueryPlan(TypedDict, total=False):
    # table 由 lock_query_plan 从 tables[0] 推导，不作为独立概念暴露给 Advisor
    table: str
    tables: list[str]

    # ── 业务方案字段（Advisor/Planner 只写这 3 个，模板简化契约）──
    # 查看字段：度量+维度+明细字段统一收口，元素可为裸字段名或 db.table.field 完整路径
    select_fields: list[str]
    # 过滤字段（含时间条件）：字符串形式，如 "create_time 今年 AND region_name='徐州大区'"
    filters: str
    # 明细型查询（不聚合、无 GROUP BY），由 Advisor 显式确认或指标 query_type=detail 派生
    detail_query: bool

    # ── 程序派生的执行字段（锁定后由 select_fields/filters 推导，不要求手工维护）──
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

    # 方案状态（统一为 confirmed）与审计时间
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

    # 宽松模式（共享方案/澄清阶段）：允许槽位为空，只做最小结构校验；
    # 严格模式（require_confirmed=True）在 lock 后调用，要求完整可执行字段。
    if not require_confirmed:
        loose_errors = []
        loose_tables = plan.get("tables") or []
        loose_select_fields = plan.get("select_fields") or []
        if not isinstance(loose_select_fields, list) or any(
            not isinstance(item, str) for item in loose_select_fields
        ):
            loose_errors.append("select_fields 必须是字符串列表")
        if (
            not loose_tables
            and not loose_select_fields
            and not str(plan.get("filters") or "").strip()
        ):
            loose_errors.append("查询方案至少需要数据表、查看字段或过滤条件")
        return loose_errors

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
    # 明细查询（detail_query）直接返回明细行，允许两者同时为空
    if not measures and not dimensions and not plan.get("detail_query"):
        # minimal 聚合方案允许度量/维度为空：select_fields/filters 由 generate_sql 按 effective_query 生成
        if not (plan.get("select_fields") or str(plan.get("filters") or "").strip()):
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

    # 指标解析证据为可选审计字段，存在时必须是字典
    concept_resolutions = plan.get("concept_resolutions")
    if concept_resolutions is not None and not isinstance(concept_resolutions, dict):
        errors.append("concept_resolutions 必须是字典")

    return errors
