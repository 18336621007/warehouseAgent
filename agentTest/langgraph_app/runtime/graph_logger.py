# 简单日志工具，统一打印节点执行信息

def log_node_event(node_name, message):
    print(f"[LangGraph][{node_name}] {message}")