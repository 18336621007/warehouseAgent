import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
path = r'agentTest/langgraph_app/graphs/supervisor_graph.py'
src = open(path, encoding='utf-8').read()
func_block = '''def plan_error_fallback_node(state):
    """Seeker 方案不可行且修复机会耗尽时，把具体失败原因转成给用户的最终答复。"""
    error = state.get("seeker_plan_error") or "当前查询无法安全执行"
    final_answer = "很抱歉，当前查询无法安全执行。\\n\\n" + error
    request_id = state.get("request_id", "")
    return {
        "final_answer": final_answer,
        "topic_status": "completed",
        "messages": [
            AIMessage(
                content=final_answer,
                name="seeker",
                id=f"{request_id}:seeker",
            )
        ],
    }

'''
assert src.count(func_block) == 1, f'c={src.count(func_block)}'
src = src.replace(func_block, "")

anchor = "from agentTest.langgraph_app.routers import route_after_planner"
# 实际 anchor 是两行 import
anchor2 = "from agentTest.langgraph_app.routers.seeker_router import route_after_seeker\n"
assert src.count(anchor2) == 1, f'a2={src.count(anchor2)}'
src = src.replace(anchor2, anchor2 + "\n\n" + func_block)
open(path, 'w', encoding='utf-8', newline='\n').write(src)
print('moved function after imports')
