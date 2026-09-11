# 执行链（原 Seeker 子图）内/父图路由：处理方案不可行、0 行自愈与执行结果回看
from agentTest.config.advisor import MAX_PLAN_REPAIR_ROUNDS


def route_after_schema(state):
    """执行链内：retrieve_schema 后若已置 seeker_plan_error 则短路结束，
    否则继续 generate_sql。"""
    if state.get("seeker_plan_error"):
        return "plan_error"
    return "generate"


def route_after_seeker(state):
    """Supervisor 父图：执行链完成后判断下一步。

    - 方案不可行（缺 join 契约等不可修复）→ fallback（告知用户，不绕 Planner repair）
    - 方案不可行但修复轮次未耗尽 → repair（回 planner 给一次调整机会）
    - 0 行自愈：执行成功但无数据 → empty_self_heal（回 planner 用 probe_values 核实）
    - 执行完成且结果非空 → review（回 planner 基于落盘结果撰写最终回答）
    - 其余（SQL 校验/执行失败重试耗尽，错误文本已由执行链写出）→ end
    """
    if state.get("seeker_plan_error"):
        if state.get("seeker_error_unresolvable"):
            return "fallback"
        if (state.get("plan_repair_rounds") or 0) < MAX_PLAN_REPAIR_ROUNDS:
            return "repair"
        return "fallback"
    # 0 行自愈优先于执行回看（两者互斥，顺序兜底）
    if state.get("seeker_empty_result"):
        return "empty_self_heal"
    if state.get("execution_review"):
        return "review"
    return "end"
