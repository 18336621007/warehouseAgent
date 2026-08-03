# Planner → Advisor 交接状态契约，显式传递 Planner 的判定依据
# 确保跨子图时 planner_reason 和 planner_entities 不被 State Schema 过滤
from typing import TypedDict


class PlannerHandoffState(TypedDict, total=False):
    planner_reason: str       # 路由原因
    planner_entities: dict    # Planner 语义分析结果（有效需求、候选表字段、完整度等）{effective_query, tables, fields, completeness}