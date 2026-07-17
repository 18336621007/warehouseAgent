# Graph 日志工具，统一写入本地日志文件
from datetime import datetime
from pathlib import Path
from time import perf_counter

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_FILE = LOG_DIR / "langgraph_app.log"


def _short_text(value, max_length=120):
    # 将长文本截断，避免日志内容过长
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


def _write_log(line):
    # 统一写入日志文件
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {line}\n")


def clear_log_file():
    # 清空旧日志文件，便于重新观察本次执行
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("", encoding="utf-8")


def get_log_file_path():
    # 返回当前日志文件路径
    return str(LOG_FILE)


def start_timer():
    # 返回当前计时起点
    return perf_counter()


def elapsed_ms(start_time):
    # 计算耗时毫秒数
    return round((perf_counter() - start_time) * 1000, 2)


def log_node_start(node_name, **kwargs):
    # 记录节点开始日志
    message = _format_kv_pairs(kwargs)
    _write_log(f"[LangGraph][INFO][{node_name}][start] {message}")


def log_node_end(node_name, **kwargs):
    # 记录节点结束日志
    message = _format_kv_pairs(kwargs)
    _write_log(f"[LangGraph][INFO][{node_name}][end] {message}")


def log_node_error(node_name, **kwargs):
    # 记录节点异常日志
    message = _format_kv_pairs(kwargs)
    _write_log(f"[LangGraph][ERROR][{node_name}] {message}")


def log_node_event(node_name, message):
    # 兼容旧写法，后续可以逐步替换掉
    _write_log(f"[LangGraph][INFO][{node_name}] {_short_text(message)}")


def log_route_decision(route_name, **kwargs):
    # 记录路由决策日志
    message = _format_kv_pairs(kwargs)
    _write_log(f"[LangGraph][INFO][{route_name}][route] {message}")
