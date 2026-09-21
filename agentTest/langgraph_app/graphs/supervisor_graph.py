# Supervisor 父图：单 Agent（Codex 模式）入口
# M4a：Planner 是唯一 Agent，Seeker 执行链封装为 execute_query 工具在循环内自主调用；
#      工具结果回填上下文后 Planner 直接写回答，不再有第二次 Planner / Evaluator / 回环路由。
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.nodes.planner_node import build_planner_node
from pathlib import Path
import sqlite3

# checkpoint 落盘路径（与 example_faiss_index 同目录）：agentTest/langgraph_app/cache/checkpoints.db
_CHECKPOINT_DB = str(Path(__file__).resolve().parents[1] / "cache" / "checkpoints.db")
from langgraph.checkpoint.sqlite import SqliteSaver
from agentTest.langgraph_app.nodes.capture_user_message_node import capture_user_message_node
from agentTest.langgraph_app.tools.registry import ToolSpec, ToolSecurity


def build_supervisor_graph(runtime):
    # execute_query 工具内部内联执行原 Seeker 执行链（方案 B：不再嵌套子图）
    from agentTest.langgraph_app.tools.execute_query_tool import build_execute_query_tool
    execute_query_tool = build_execute_query_tool(runtime)
    runtime["tool_registry"].register(ToolSpec(
        name="execute_query",
        description=execute_query_tool.description,
        tool=execute_query_tool,
        groups=("planner",),
        security=ToolSecurity(read_only=True, row_limit=0, whitelist_only=True),
    ))

    # 父图使用同一个 AgentState（Planner 单 Agent，执行链在工具内部）
    supervisor = StateGraph(AgentState)

    # 统一记录本轮用户输入，再交给 Planner 判断
    supervisor.add_node("capture_user_message", capture_user_message_node)
    supervisor.add_node("planner", build_planner_node(runtime))

    # 单 Agent：Planner 循环内检索/查数/写回答，输出 respond_text（最终回答）后直接结束
    supervisor.add_edge(START, "capture_user_message")
    supervisor.add_edge("capture_user_message", "planner")
    supervisor.add_edge("planner", END)

    # checkpoint 落盘到本地 sqlite（check_same_thread=False：Flask 后台线程并发访问 checkpoint）
    # 替代内存 MemorySaver：释放内存 + 跨重启保留会话历史
    checkpointer = SqliteSaver(sqlite3.connect(_CHECKPOINT_DB, check_same_thread=False))
    return supervisor.compile(checkpointer=checkpointer)
