# Planner 判定依据状态，显式传递路由原因（跨子图不被 State Schema 过滤）
from typing import TypedDict


class PlannerHandoffState(TypedDict, total=False):
    planner_reason: str       # 路由原因