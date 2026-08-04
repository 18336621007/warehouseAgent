# 查询方案领域服务，集中处理方案锁定、确认和校验
from copy import deepcopy
from datetime import datetime

from agentTest.langgraph_app.state.query_plan import QueryPlan
from agentTest.langgraph_app.state.query_plan import validate_query_plan


def _deduplicate(values: list[str]) -> list[str]:
    """按照原顺序删除重复字段。"""
    result = []
    seen = set()

    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)

    return result


def lock_query_plan(proposed_plan: dict) -> QueryPlan:
    """将 Advisor 生成的完整方案标准化为 locked 方案。table 从 tables[0] 推导。"""
    plan = deepcopy(proposed_plan)

    measures = plan.get("measures") or []
    dimensions = plan.get("dimensions") or []
    time_field = plan.get("time_field", "")
    advisors_tables = plan.get("tables") or []
    advisors_field_sources = plan.get("field_sources") or []  # ["db.table.field", ...]

    if advisors_field_sources:
        # 从 "db.table.field" 字符串提取 {field: table} 映射
        source_map: dict[str, str] = {}
        derived_tables: list[str] = []
        for fs in advisors_field_sources:
            parts = fs.rsplit(".", 1)
            if len(parts) != 2:
                continue
            table_name = parts[0]   # ads_trip.xxx
            field_name = parts[1]   # company_name
            source_map[field_name] = table_name
            if table_name not in derived_tables:
                derived_tables.append(table_name)
        plan["tables"] = derived_tables
        plan["field_sources"] = source_map                      # {field: table} dict
    else:
        plan["tables"] = advisors_tables

    # table 统一从 tables[0] 推导
    plan["table"] = plan["tables"][0] if plan["tables"] else ""

    plan.pop("_field_sources", None)

    fields = list(measures) + list(dimensions)
    if time_field:
        fields.append(time_field)

    plan["fields"] = _deduplicate(fields)
    # optional fields passthrough
    if "having" not in plan:
        plan["having"] = ""
    if "order_by" not in plan:
        plan["order_by"] = []
    if "result_limit" not in plan:
        plan["result_limit"] = 1000
    if "complex" not in plan:
        plan["complex"] = False
    # table_plans: 自动为所有涉及的表生成独立子方案
    advisors_table_plans = plan.get("table_plans") or []
    if not advisors_table_plans:
        # Advisor 未提供时，自动为 tables 中的每张表生成，共享 time_field/time_range/filters
        all_tables = plan.get("tables", [])
        shared_time = plan.get("time_field", "pt_dt")
        shared_range = plan.get("time_range", "昨天")
        shared_filters = plan.get("filters", "")
        plan["table_plans"] = [
            {
                "table": t,
                "time_field": shared_time,
                "time_range": shared_range,
                "filters": shared_filters
            }
            for t in all_tables
        ]
    else:
        plan["table_plans"] = advisors_table_plans
    plan["status"] = "locked"
    plan["locked_at"] = datetime.now().isoformat()
    plan.pop("confirmed_at", None)

    errors = validate_query_plan(plan)
    if errors:
        raise ValueError("查询方案不完整：" + "；".join(errors))

    return plan


def confirm_query_plan(current_plan: QueryPlan) -> QueryPlan:
    """最终确认 locked 方案，只有确认后才能交给 Seeker。"""
    if not current_plan:
        raise ValueError("当前不存在可确认的查询方案")

    if current_plan.get("status") != "locked":
        raise ValueError("只有 locked 状态的查询方案才能最终确认")

    errors = validate_query_plan(current_plan)
    if errors:
        raise ValueError("查询方案校验失败：" + "；".join(errors))

    plan = deepcopy(current_plan)
    plan["status"] = "confirmed"
    plan["confirmed_at"] = datetime.now().isoformat()
    return plan