# Graph结构化日志工具，统一记录请求、节点和路由事件
import json
import logging
import threading
import traceback
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter
from agentTest.config import log_config


LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_FILE = LOG_DIR / "langgraph_app.jsonl"

# 单条文本字段的最大长度，避免SQL和模型输出撑爆日志文件
_MAX_TEXT_LENGTH = 2000

# 事件分类：每类日志挂稳定的 category，便于 trace_view 按类别过滤
_EVENT_CATEGORY = {
    "request.started": "lifecycle",
    "request.completed": "lifecycle",
    "request.failed": "lifecycle",
    "request.user_input": "lifecycle",
    "cli.round.started": "lifecycle",
    "node.started": "lifecycle",
    "node.completed": "lifecycle",
    "node.failed": "lifecycle",
    "node.degraded": "lifecycle",
    "node.event": "lifecycle",
    "node.detail": "lifecycle",
    "state.changed": "state",
    "state.snapshot": "state",
    "llm.call": "llm",
    "llm.response": "llm",
    "llm.error": "llm",
    "route.decided": "plan",
    "plan.locked": "plan",
    "advisor.mode": "plan",
    "metric_ambiguity.detected": "metric",
    "metric_resolution.user_required": "metric",
    "metric_resolution.completed": "metric",
    "candidate_recall": "metric",
    "candidate_rerank.selected": "metric",
    "example.retrieved": "search",
    "tools.called": "search",
    "search.scores": "search",
}

# 当前请求内的 LLM 调用计数（跨线程共享）：LLM 调用可能在子线程执行，
# 请求结束时在主线程读取，因此按 request_id 聚合而不是用线程局部变量
_LLM_CALL_COUNTS: dict = {}
_LLM_CALL_LOCK = threading.Lock()

# 保存当前请求的日志身份，不同并发请求之间相互隔离
_LOG_CONTEXT = ContextVar(
    "graph_log_context",
    default={},
)

# 保存当前请求的 Span 栈与事件序号，用于生成树形 trace
_SPAN_STACK = ContextVar(
    "graph_span_stack",
    default=[],
)

_SEQ = ContextVar(
    "graph_seq",
    default=0,
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
    _SPAN_STACK.set([])
    _SEQ.set(0)

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


def _clip_text(value, max_length=0):
    # 保留换行的文本裁剪，max_length<=0 表示不裁剪
    text = str(value)
    if max_length and len(text) > max_length:
        return text[:max_length] + "..."
    return text


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


def _next_seq():
    # 请求内事件序号单调递增，保证 trace 渲染排序稳定
    value = _SEQ.get() + 1
    _SEQ.set(value)
    return value


def _current_span_id():
    # 返回当前栈顶 span_id，无上下文时返回空串
    stack = _SPAN_STACK.get()
    return stack[-1]["span_id"] if stack else ""


def _pop_span(node_name):
    # 弹出指定节点最近的 span，返回 (span_id, parent_span_id)
    stack = list(_SPAN_STACK.get())
    for index in range(len(stack) - 1, -1, -1):
        if stack[index]["name"] == node_name:
            span_id = stack[index]["span_id"]
            remaining = stack[:index]
            _SPAN_STACK.set(remaining)
            parent_span_id = remaining[-1]["span_id"] if remaining else ""
            return span_id, parent_span_id
    return "", _current_span_id()


def _write_log(level, event, node_name="", **kwargs):
    # 每行写入一个独立JSON对象，方便按字段检索
    seq = _next_seq()
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(
            timespec="milliseconds",
        ),
        "level": logging.getLevelName(level),
        "event": event,
        "seq": seq,
        "category": kwargs.pop("category", "") or _EVENT_CATEGORY.get(event, "other"),
    }

    log_context = _LOG_CONTEXT.get()
    for key, value in log_context.items():
        payload[key] = _normalize_value(value)

    # trace 链路字段：request_id 作为 trace_id，普通事件挂到当前 span 下
    span_id = kwargs.pop("span_id", "") or f"evt#{seq}"
    parent_span_id = kwargs.pop("parent_span_id", None)
    if parent_span_id is None:
        parent_span_id = _current_span_id()
    payload["trace_id"] = (
        log_context.get("request_id")
        or log_context.get("conversation_id")
        or ""
    )
    payload["span_id"] = span_id
    payload["parent_span_id"] = parent_span_id

    if node_name:
        payload["node"] = node_name

    for key, value in kwargs.items():
        # 核心字段不允许被业务日志参数覆盖
        if key in payload:
            continue

        # 异常堆栈保留完整内容，不使用普通文本截断规则
        if key in ("stack_trace", "prompt", "output"):
            max_length = (
                log_config.LOG_LLM_MAX_LENGTH
                if key in ("prompt", "output")
                else 0
            )
            payload[key] = _clip_text(value, max_length)
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


def log_sub_info(message, node_name=""):
    # 将原来的缩进补充信息改成可检索的结构化事件
    _write_log(
        logging.INFO,
        "node.detail",
        node_name=node_name,
        message=message,
    )


def log_node_start(node_name, **kwargs):
    # 记录节点开始执行并压入 Span 栈
    span_id = f"{node_name}#{_next_seq()}"
    parent_span_id = _current_span_id()
    stack = list(_SPAN_STACK.get())
    stack.append({"name": node_name, "span_id": span_id})
    _SPAN_STACK.set(stack)
    _write_log(
        logging.INFO,
        "node.started",
        node_name=node_name,
        span_id=span_id,
        parent_span_id=parent_span_id,
        **kwargs,
    )


def log_node_end(node_name, **kwargs):
    # 记录节点正常完成并弹出 Span
    span_id, parent_span_id = _pop_span(node_name)
    _write_log(
        logging.INFO,
        "node.completed",
        node_name=node_name,
        span_id=span_id or f"{node_name}#{_next_seq()}",
        parent_span_id=parent_span_id,
        **kwargs,
    )


def log_node_error(node_name, **kwargs):
    # 记录节点执行失败并弹出 Span
    span_id, parent_span_id = _pop_span(node_name)
    _write_log(
        logging.ERROR,
        "node.failed",
        node_name=node_name,
        span_id=span_id or f"{node_name}#{_next_seq()}",
        parent_span_id=parent_span_id,
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
    # 请求根 Span：重置栈与序号，后续节点都挂到 request 下
    _SPAN_STACK.set([{"name": "request", "span_id": "request#0"}])
    _SEQ.set(0)
    _write_log(
        logging.INFO,
        "request.started",
        span_id="request#0",
        parent_span_id="",
        **kwargs,
    )


def log_request_end(**kwargs):
    # 请求完成：清空 Span 栈，避免残留影响下一请求
    # 请求级摘要：调用方补充节点数等统计，LLM 调用次数由日志层自动统计
    summary = dict(kwargs.pop("summary", {}) or {})
    summary.setdefault("llm_calls", get_llm_call_count())
    clear_llm_call_count()
    _write_log(
        logging.INFO,
        "request.completed",
        span_id="request#0",
        parent_span_id="",
        summary=summary,
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

    _SPAN_STACK.set([])
    _write_log(
        logging.ERROR,
        "request.failed",
        span_id="request#0",
        parent_span_id="",
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

    span_id, parent_span_id = _pop_span(node_name)
    _write_log(
        logging.WARNING,
        "node.degraded",
        node_name=node_name,
        span_id=span_id or f"{node_name}#{_next_seq()}",
        parent_span_id=parent_span_id,
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


def log_metric_event(event, **kwargs):
    """指标/语义层结构化日志，event 为 metric_ambiguity.detected / semantic.match 等稳定事件名。
    默认归属 metric_ambiguity 节点，可通过 node_name 覆盖（如 planner / advisor）。"""
    node_name = kwargs.pop("node_name", "metric_ambiguity")
    _write_log(
        logging.INFO,
        event,
        node_name=node_name,
        category="metric",
        **kwargs,
    )


def log_tools_called(node_name, tools):
    # 记录节点本轮实际调用的工具列表，替代自由文本“本轮工具调用”
    _write_log(
        logging.INFO,
        "tools.called",
        node_name=node_name,
        tools=[
            _short_text(tool, max_length=60)
            for tool in tools
        ],
    )


def log_example_retrieved(node_name, hit_count, top_sim="", top_question="", hint=""):
    # 记录历史示例检索结果，hint 说明注入边界（如指标未解析不注入SQL）
    _write_log(
        logging.INFO,
        "example.retrieved",
        node_name=node_name,
        hit_count=hit_count,
        top_sim=top_sim,
        top_question=_short_text(top_question, max_length=200),
        hint=_short_text(hint, max_length=100),
    )


def log_plan_locked(node_name, table, measures, dimensions, order_by=None, result_limit=1000, table_plans=None):
    # 记录最终锁定的查询方案，替代自由文本 locked_plan 摘要
    _write_log(
        logging.INFO,
        "plan.locked",
        node_name=node_name,
        table=table,
        measures=measures,
        dimensions=dimensions,
        order_by=order_by or [],
        result_limit=result_limit,
        table_plans=table_plans or [],
    )


def log_advisor_mode(node_name, mode, completeness):
    # 记录 Advisor 本轮运行模式与 Planner 模糊度
    _write_log(
        logging.INFO,
        "advisor.mode",
        node_name=node_name,
        mode=mode,
        completeness=completeness,
    )

def _current_request_id():
    # 返回当前日志上下文的 request_id，无上下文返回空串
    return _LOG_CONTEXT.get().get("request_id", "") or _LOG_CONTEXT.get().get("conversation_id", "")


def get_llm_call_count():
    # 返回当前请求内的 LLM 调用次数（由 llm 日志回调跨线程递增）
    request_id = _current_request_id()
    if not request_id:
        return 0
    with _LLM_CALL_LOCK:
        return _LLM_CALL_COUNTS.get(request_id, 0)


def incr_llm_call_count():
    # LLM 日志回调每次调用时递增（按 request_id 聚合，跨线程安全）
    request_id = _current_request_id()
    if not request_id:
        return
    with _LLM_CALL_LOCK:
        _LLM_CALL_COUNTS[request_id] = _LLM_CALL_COUNTS.get(request_id, 0) + 1


def clear_llm_call_count():
    # 请求结束后清理计数，避免内存增长
    request_id = _current_request_id()
    if not request_id:
        return
    with _LLM_CALL_LOCK:
        _LLM_CALL_COUNTS.pop(request_id, None)


def _plan_summary(plan):
    # 查询方案摘要：只保留结构化关键字段，避免大对象写入日志
    if not plan:
        return {}
    return {
        "status": plan.get("status", ""),
        "table": plan.get("table", ""),
        "tables": plan.get("tables") or [],
        "measures": plan.get("measures") or [],
        "dimensions": plan.get("dimensions") or [],
        "time_field": plan.get("time_field", ""),
        "time_range": plan.get("time_range", ""),
        "filters": plan.get("filters", ""),
    }


def _resolution_summary(resolutions):
    # 指标/维度解析证据摘要，只保留状态、命中字段与候选数量
    top_n = log_config.LOG_STATE_TOP_N
    summary = []
    for item in (resolutions or [])[:top_n]:
        summary.append({
            "mention": item.get("mention", ""),
            "status": item.get("status", ""),
            "selected_field": item.get("selected_field", ""),
            "selected_table": item.get("selected_table", ""),
            "source": item.get("resolution_source", ""),
            "candidate_count": len(item.get("candidates") or []),
        })
    return summary


def _spec_summary(spec):
    # AnalysisSpec 摘要：业务概念与解析证据只保留 Top-N
    if not spec:
        return {}
    return {
        "analysis_type": spec.get("analysis_type", ""),
        "metric_mentions": spec.get("metric_mentions") or [],
        "dimension_mentions": spec.get("dimension_mentions") or [],
        "time_range": spec.get("time_range", ""),
        "time_grain": spec.get("time_grain", ""),
        "order_by": spec.get("order_by") or [],
        "metric_resolutions": _resolution_summary(spec.get("metric_resolutions")),
        "dimension_resolutions": _resolution_summary(spec.get("dimension_resolutions")),
    }


def build_state_snapshot(state, node_name=""):
    # 从共享 State 提取分层摘要：共享层（route/topic/方案/分析意图）+ Agent 层关键字段
    snapshot = {}
    snapshot["route"] = state.get("route", "")
    snapshot["topic_status"] = state.get("topic_status", "")
    snapshot["follow_up_mode"] = state.get("follow_up_mode", "")
    snapshot["advisor_turns"] = state.get("advisor_turns", 0)
    snapshot["effective_query"] = _short_text(
        state.get("effective_query", ""),
        max_length=300,
    )
    snapshot["confirmed_plan"] = _plan_summary(state.get("confirmed_plan") or {})
    snapshot["analysis_spec"] = _spec_summary(state.get("analysis_spec") or {})

    # Agent 层：Planner 实体与原因
    entities = state.get("planner_entities") or {}
    if entities:
        snapshot["planner_entities"] = {
            "table": entities.get("table", ""),
            "tables": entities.get("tables") or [],
            "fields": entities.get("fields") or [],
            "completeness": entities.get("completeness", ""),
        }
    if state.get("planner_reason"):
        snapshot["planner_reason"] = _short_text(
            state.get("planner_reason", ""),
            max_length=300,
        )

    # Agent 层：Advisor/Seeker/Evaluator 关键输出
    if state.get("final_answer"):
        snapshot["final_answer"] = _short_text(
            state.get("final_answer", ""),
            max_length=300,
        )
    if state.get("generated_sql"):
        snapshot["generated_sql"] = _short_text(
            state.get("generated_sql", ""),
            max_length=500,
        )
    if state.get("sql_valid") is not None:
        snapshot["sql_valid"] = state.get("sql_valid")
    if state.get("sql_error"):
        snapshot["sql_error"] = _short_text(
            state.get("sql_error", ""),
            max_length=300,
        )
    last_result = state.get("last_query_result") or {}
    if last_result:
        snapshot["last_query_result"] = {
            "row_count": last_result.get("row_count", 0),
            "columns": (last_result.get("columns") or [])[:5],
            "result_summary": _short_text(
                last_result.get("result_summary", ""),
                max_length=200,
            ),
        }
    if state.get("evaluator_score") is not None:
        snapshot["evaluator_score"] = state.get("evaluator_score")
    return snapshot


def log_state_snapshot(node_name, state, **extra):
    # 记录 Agent 节点执行后的 State 分层摘要（共享层 + Agent 层）
    snapshot = build_state_snapshot(state, node_name)
    _write_log(
        logging.INFO,
        "state.snapshot",
        node_name=node_name,
        category="state",
        state=snapshot,
        **extra,
    )


def log_llm_call(caller, model, prompts, call_id="", **extra):
    # 记录 LLM 调用开始（含 prompt 摘要），供 trace 回放模型输入
    if not log_config.LOG_LLM_ENABLED:
        return
    incr_llm_call_count()
    _write_log(
        logging.INFO,
        "llm.call",
        node_name=caller,
        category="llm",
        model=model,
        call_id=call_id,
        prompt=_clip_text(
            "\n".join(str(item) for item in (prompts or [])),
            log_config.LOG_LLM_MAX_LENGTH,
        ),
        **extra,
    )


def log_llm_response(caller, model, output, ms, tokens=None, call_id="", **extra):
    # 记录 LLM 返回结果：输出、耗时与 token 用量
    if not log_config.LOG_LLM_ENABLED:
        return
    _write_log(
        logging.INFO,
        "llm.response",
        node_name=caller,
        category="llm",
        model=model,
        call_id=call_id,
        output=_clip_text(output or "", log_config.LOG_LLM_MAX_LENGTH),
        ms=ms,
        tokens=tokens or {},
        **extra,
    )


def log_llm_error(caller, model, error, ms, call_id="", **extra):
    # 记录 LLM 调用失败，不影响主流程继续执行
    if not log_config.LOG_LLM_ENABLED:
        return
    _write_log(
        logging.WARNING,
        "llm.error",
        node_name=caller,
        category="llm",
        model=model,
        call_id=call_id,
        error_message=_short_text(error, max_length=1000),
        ms=ms,
        **extra,
    )


def log_search_scores(node_name, layer, scores):
    # 记录检索评分列表（结构化），替代原来的自由文本表/字段评分
    _write_log(
        logging.INFO,
        "search.scores",
        node_name=node_name,
        category="search",
        layer=layer,
        scores=[
            {
                "name": _short_text(item.get("name", ""), max_length=80),
                "score": item.get("score", 0),
            }
            for item in (scores or [])
        ],
    )
