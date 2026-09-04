# Seeker 子图内/父图路由：处理方案不可行时的回退与修复
from agentTest.config.advisor import MAX_PLAN_REPAIR_ROUNDS


def route_after_schema(state):
    """Seeker 子图内：retrieve_schema 后若已置 seeker_plan_error 则短路结束，
    否则继续 generate_sql。"""
    if state.get("seeker_plan_error"):
        return "plan_error"
    return "generate"


def route_after_seeker(state):
    """Supervisor 父图：Seeker 完成后判断是否需要回 Planner 修复。

    - 有 seeker_plan_error 且修复轮次未耗尽 → 回 planner（给 LLM 一次调整方案的机会）
    - 有 seeker_plan_error 但轮次耗尽 → 走 fallback（给用户具体失败原因）
    - 正常完成 → END
    """
    if state.get("seeker_plan_error"):
        if (state.get("plan_repair_rounds") or 0) < MAX_PLAN_REPAIR_ROUNDS:
            return "repair"
        return "fallback"
    return "end"
