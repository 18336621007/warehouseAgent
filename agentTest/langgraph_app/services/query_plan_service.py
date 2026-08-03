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
    """将 Advisor 生成的完整方案标准化为 locked 方案。"""
    plan = deepcopy(proposed_plan)

    table = plan.get("table", "")
    measures = plan.get("measures") or []
    dimensions = plan.get("dimensions") or []
    time_field = plan.get("time_field", "")

    # 根据字段来源推导参与表，不再固定为单表
    # 当前字段来源由 Advisor 在 submit_query_plan 时通过 search_columns 的 metadata 提供
    field_sources = plan.get("_field_sources") or {}
    derived_tables = list(dict.fromkeys(field_sources.values()))  # 去重保留顺序
    if derived_tables:
        plan["tables"] = derived_tables
    else:
        # 无字段来源信息时回退为单表，保持向后兼容
        plan["tables"] = [table] if table else []
    # _field_sources 是内部传递用，不写入最终 QueryPlan
    plan.pop("_field_sources", None)

    fields = list(measures) + list(dimensions)
    if time_field:
        fields.append(time_field)

    plan["fields"] = _deduplicate(fields)
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