# 执行链（原 Seeker 子图）内路由：处理方案不可行短路
def route_after_schema(state):
    """执行链内：retrieve_schema 后若已置 seeker_plan_error 则短路结束，
    否则继续 generate_sql。"""
    if state.get("seeker_plan_error"):
        return "plan_error"
    return "generate"
