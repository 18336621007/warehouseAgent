# Graph 日志工具，统一写入本地日志文件
# 格式 A（紧凑型）：HH:MM:SS node  icon message，轮次间用分隔线
from datetime import datetime
from pathlib import Path
from time import perf_counter

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_FILE = LOG_DIR / "langgraph_app.log"

# 节点名对齐宽度，保证 icon 列对齐
_NODE_WIDTH = 12


def _short_text(value, max_length=200):
    # 将长文本截断，避免日志内容过长（默认 120 字符，SQL 类可传更大值）
    if value is None:
        return ""

    text = str(value).replace("\n", " ").strip()
    if len(text) <= max_length:
        return text

    return text[:max_length] + "..."


def _format_inline(data):
    # 将字典格式化成单行 key=value | key=value 片段
    if not data:
        return ""

    parts = []
    for key, value in data.items():
        parts.append(f"{key}={_short_text(value)}")

    return " | ".join(parts)


def _write_log(node_name, icon, message):
    # 统一写入日志文件：HH:MM:SS node_name(12宽) icon message
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H:%M:%S")
    padded = node_name.ljust(_NODE_WIDTH)
    line = f"{timestamp} {padded} {icon} {message}".rstrip()
    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


# ── 公开 API ──

def clear_log_file():
    # 清空旧日志文件，便于重新观察本次执行
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("", encoding="utf-8")


def get_log_file_path():
    # 返回当前日志文件路径
    return str(LOG_FILE)


def start_timer():
    # 返回当前计时起点（秒级浮点）
    return perf_counter()


def elapsed_ms(start_time):
    # 计算耗时毫秒数
    return round((perf_counter() - start_time) * 1000, 2)


def log_round_separator(round_num):
    # 轮次分隔线：══════ 第N轮 ══════
    _write_log("", "═", f"═════ 第{round_num}轮 {'═' * 50}")


def log_sub_info(message):
    # 辅助信息缩进行：不显示时间，对齐到 message 列
    indent = " " * (8 + 1 + _NODE_WIDTH + 2)  # timestamp(8) + space + node(12) + space + icon(1) + space
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(f"{indent}{message}\n")


def log_node_start(node_name, **kwargs):
    # 节点开始：HH:MM:SS node_name  ▶ message
    _write_log(node_name, "▶", _format_inline(kwargs))


def log_node_end(node_name, **kwargs):
    # 节点结束：HH:MM:SS node_name  ◀ message
    _write_log(node_name, "◀", _format_inline(kwargs))


def log_node_error(node_name, **kwargs):
    # 节点异常：HH:MM:SS node_name  ✖ message
    _write_log(node_name, "✖", _format_inline(kwargs))


def log_node_event(node_name, message):
    # 兼容旧写法的事件日志
    _write_log(node_name, "●", _short_text(message))


def log_route_decision(route_name, **kwargs):
    # 路由决策：HH:MM:SS route_name → message
    _write_log(route_name, "→", _format_inline(kwargs))


def log_user_input(message):
    # 用户输入：HH:MM:SS user       ▶ message
    _write_log("user", "▶", _short_text(message, max_length=200))
