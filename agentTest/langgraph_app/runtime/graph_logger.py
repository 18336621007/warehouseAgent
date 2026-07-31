# Graph结构化日志工具，统一记录请求、节点和路由事件
import json
import logging
import traceback
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter


LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_FILE = LOG_DIR / "langgraph_app.jsonl"

# 单条文本字段的最大长度，避免SQL和模型输出撑爆日志文件
_MAX_TEXT_LENGTH = 2000

# 保存当前请求的日志身份，不同并发请求之间相互隔离
_LOG_CONTEXT = ContextVar(
    "graph_log_context",
    default={},
)


def _build_logger():
    # 使用标准logging保证线程安全，并按天滚动保留日志
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("sql_mars.langgraph")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    handler = TimedRotatingFileHandler(
        filename=str(LOG_FILE),
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        delay=True,
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    return logger


_LOGGER = _build_logger()


def bind_log_context(
        conversation_id="",
        topic_id="",
        request_id="",
        graph_thread_id="",
):
    # 在请求入口绑定身份字段，返回Token供请求结束时恢复
    context = {
        "conversation_id": conversation_id,
        "topic_id": topic_id,
        "request_id": request_id,
        "graph_thread_id": graph_thread_id,
    }
    return _LOG_CONTEXT.set({
        key: value
        for key, value in context.items()
        if value
    })


def reset_log_context(token):
    # 请求结束后释放上下文，防止后续请求继承错误身份
    _LOG_CONTEXT.reset(token)


def _short_text(value, max_length=_MAX_TEXT_LENGTH):
    # 将长文本压缩为单行并限制长度
    if value is None:
        return ""

    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_length:
        return text

    return text[:max_length] + "..."


def _normalize_value(value):
    # 保留数字和布尔类型，并安全转换复杂对象
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return _short_text(value)

    if isinstance(value, dict):
        return {
            str(key): _normalize_value(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _normalize_value(item)
            for item in value
        ]

    return _short_text(value)


def _write_log(level, event, node_name="", **kwargs):
    # 每行写入一个独立JSON对象，方便按字段检索
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(
            timespec="milliseconds",
        ),
        "level": logging.getLevelName(level),
        "event": event,
    }

    for key, value in _LOG_CONTEXT.get().items():
        payload[key] = _normalize_value(value)

    if node_name:
        payload["node"] = node_name

    for key, value in kwargs.items():
        # 核心字段不允许被业务日志参数覆盖
        if key in payload:
            continue

        # 异常堆栈保留完整内容，不使用普通文本截断规则
        if key == "stack_trace":
            payload[key] = str(value)
        else:
            payload[key] = _normalize_value(value)

    _LOGGER.log(
        level,
        json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        ),
    )


def clear_log_file():
    # 保留旧接口兼容性，线上日志不再由程序启动时清空
    return None


def get_log_file_path():
    # 返回当前结构化日志文件路径
    return str(LOG_FILE)


def start_timer():
    # 返回当前高精度计时起点
    return perf_counter()


def elapsed_ms(start_time):
    # 计算节点耗时，单位为毫秒
    return round(
        (perf_counter() - start_time) * 1000,
        2,
    )


def log_round_separator(round_num):
    # 记录CLI新一轮对话开始
    _write_log(
        logging.INFO,
        "cli.round.started",
        round_num=round_num,
    )


def log_sub_info(message):
    # 将原来的缩进补充信息改成可检索的结构化事件
    _write_log(
        logging.INFO,
        "node.detail",
        message=message,
    )


def log_node_start(node_name, **kwargs):
    # 记录节点开始执行
    _write_log(
        logging.INFO,
        "node.started",
        node_name=node_name,
        **kwargs,
    )


def log_node_end(node_name, **kwargs):
    # 记录节点正常完成
    _write_log(
        logging.INFO,
        "node.completed",
        node_name=node_name,
        **kwargs,
    )


def log_node_error(node_name, **kwargs):
    # 记录节点执行失败
    _write_log(
        logging.ERROR,
        "node.failed",
        node_name=node_name,
        **kwargs,
    )


def log_node_event(node_name, message):
    # 记录节点执行过程中的普通事件
    _write_log(
        logging.INFO,
        "node.event",
        node_name=node_name,
        message=message,
    )


def log_route_decision(route_name, **kwargs):
    # 记录路由器的最终分支选择
    _write_log(
        logging.INFO,
        "route.decided",
        node_name=route_name,
        **kwargs,
    )


def log_user_input(message):
    # 记录CLI用户输入，限制长度避免保存过多敏感内容
    _write_log(
        logging.INFO,
        "request.user_input",
        message=_short_text(
            message,
            max_length=500,
        ),
    )

def log_request_start(**kwargs):
    # 记录一次请求开始处理
    _write_log(
        logging.INFO,
        "request.started",
        **kwargs,
    )


def log_request_end(**kwargs):
    # 记录一次请求正常处理完成
    _write_log(
        logging.INFO,
        "request.completed",
        **kwargs,
    )


def log_request_error(
        error,
        error_id,
        error_code="QUERY_EXECUTION_FAILED",
        **kwargs,
):
    # 未处理异常只在请求边界记录完整堆栈和统一错误编号
    stack_trace = "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__,
        )
    )

    _write_log(
        logging.ERROR,
        "request.failed",
        error_id=error_id,
        error_code=error_code,
        error_type=type(error).__name__,
        error_message=_short_text(
            error,
            max_length=1000,
        ),
        stack_trace=stack_trace,
        **kwargs,
    )


def log_node_degraded(
        node_name,
        error,
        **kwargs,
):
    # 非核心能力异常只记录降级，不中断主查询流程
    stack_trace = "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__,
        )
    )

    _write_log(
        logging.WARNING,
        "node.degraded",
        node_name=node_name,
        error_type=type(error).__name__,
        error_message=_short_text(
            error,
            max_length=1000,
        ),
        stack_trace=stack_trace,
        **kwargs,
    )


def log_state_change(
        node_name,
        field_name,
        previous_value,
        current_value,
):
    # 只在State字段真正发生变化时记录状态转换
    _write_log(
        logging.INFO,
        "state.changed",
        node_name=node_name,
        field=field_name,
        previous=previous_value,
        current=current_value,
    )