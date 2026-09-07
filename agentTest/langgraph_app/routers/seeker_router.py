# Seeker 子图内/父图路由：处理方案不可行时的回退与修复
from agentTest.config.advisor import MAX_PLAN_REPAIR_ROUNDS, MAX_ADVISOR_AUTO_CONTINUE


def route_after_schema(state):
    """Seeker 子图内：retrieve_schema 后若已置 seeker_plan_error 则短路结束，
    否则继续 generate_sql。"""
    if state.get("seeker_plan_error"):
        return "plan_error"
    return "generate"


def route_after_seeker(state):
    """Supervisor 父图：Seeker 完成后判断是否需要回 Planner 修复。

    - 缺 join 契约等不可修复错误 → 直接走 fallback（告知用户，不绕 Planner repair）
    - 有 seeker_plan_error 且修复轮次未耗尽 → 回 planner（给 LLM 一次调整方案的机会）
    - 有 seeker_plan_error 但轮次耗尽 → 走 fallback（给用户具体失败原因）
    - 正常完成 → END
    """
    if state.get("seeker_plan_error"):
        if state.get("seeker_error_unresolvable"):
            return "fallback"
        if (state.get("plan_repair_rounds") or 0) < MAX_PLAN_REPAIR_ROUNDS:
            return "repair"
        return "fallback"
    return "end"


def route_after_advisor(state):
    """Supervisor 父图：Advisor 结束后是否需要自动回 Planner 再判定。

    - Advisor 收尾结构化输出 next_step=return_to_planner 且自动回环未超上限 → 回 planner
    - 否则 → 结束，等待用户下一轮输入
    程序只读结构化字段，不解析最终回复文本/标点判断是否提问。
    """
    if (
        state.get("advisor_next_step") == "return_to_planner"
        and (state.get("advisor_auto_rounds") or 0) <= MAX_ADVISOR_AUTO_CONTINUE
    ):
        return "planner"
    return "end"
