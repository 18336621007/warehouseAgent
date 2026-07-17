# Graph 日志工具，统一打印节点执行摘要

def _short_text(value, max_length=120):
    # 将长文本截断，避免日志刷屏
    if value is None:
        return ""

    text = str(value).replace("\n", " ").strip()
    if len(text) <= max_length:
        return text

    return text[:max_length] + "..."


def _format_kv_pairs(data):
    # 将字典格式化成 key=value 的日志片段
    if not data:
        return ""

    parts = []
    for key, value in data.items():
        parts.append(f"{key}={_short_text(value)}")

    return ", ".join(parts)


def log_node_start(node_name, **kwargs):
    # 打印节点开始日志
    message = _format_kv_pairs(kwargs)
    print(f"[LangGraph][{node_name}][start] {message}")


def log_node_end(node_name, **kwargs):
    # 打印节点结束日志
    message = _format_kv_pairs(kwargs)
    print(f"[LangGraph][{node_name}][end] {message}")


def log_node_event(node_name, message):
    # 兼容旧写法，后续可以逐步替换掉
    print(f"[LangGraph][{node_name}] {_short_text(message)}")


def log_route_decision(route_name, **kwargs):
    # 打印路由决策日志
    message = _format_kv_pairs(kwargs)
    print(f"[LangGraph][{route_name}][route] {message}")
