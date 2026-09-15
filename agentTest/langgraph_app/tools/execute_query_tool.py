# execute_query_tool.py —— 查数工具：把 Seeker 执行链封装为一次工具调用
# 单 Agent（Codex 模式）下，Agent 在 ReAct 循环里调 execute_query 查数，
# 结果（列 + 预览 + 行数 + CSV 路径）回填上下文，Agent 直接基于结果写回答；
# 不再需要"执行后回 Planner 写回答"的第二次决策。
# 多数据源/并行预留：内部按 confirmed_plan 的 engine 取数据源（当前默认 hive），
# 工具执行器预留多 tool_calls 并行（当前串行）。
import re
from contextvars import ContextVar

from langchain.tools import tool
from agentTest.semantic_layer.metric_matcher import grep_metrics_from_keywords
from agentTest.langgraph_app.services.plan_synthesizer import build_plan_from_semantic
from agentTest.langgraph_app.runtime.graph_logger import log_metric_event
from agentTest.config.semantic import (
    SEMANTIC_UNIQUE_GAP_THRESHOLD,
    SEMANTIC_CONFIDENCE_UNIQUE,
    SEMANTIC_CONFIDENCE_CANDIDATE,
)

# 当前请求上下文：由 planner 循环入口设置，工具内部据此组装 seeker state（日志/落盘归属）
_current_request_id = ContextVar("execute_query_request_id", default="")
_current_conversation_id = ContextVar("execute_query_conversation_id", default="")
_current_topic_id = ContextVar("execute_query_topic_id", default="")

# 关键词拆分分隔符：中英文常见分隔符
_TOKEN_SPLIT = re.compile(r"[\s,，、。;；:：]+")


def set_execute_query_context(request_id: str, conversation_id: str, topic_id: str) -> tuple:
    """设置当前请求上下文，返回 contextvar tokens（调用方需 finally reset）。"""
    return (
        _current_request_id.set(str(request_id or "")),
        _current_conversation_id.set(str(conversation_id or "")),
        _current_topic_id.set(str(topic_id or "")),
    )


def reset_execute_query_context(tokens: tuple) -> None:
    """复位请求上下文，防止跨请求串会话。"""
    for _var, _token in zip(
        (_current_request_id, _current_conversation_id, _current_topic_id), tokens
    ):
        _var.reset(_token)


def _split_keywords(question: str) -> list[str]:
    """把查询句拆成独立业务检索词（剔除空串）。"""
    return [t.strip() for t in _TOKEN_SPLIT.split(str(question or "")) if t and t.strip()]


def _log_metric_hit(metric_hits, question, metric_source):
    """记录 execute_query 工具内部的真实语义层命中（与 Planner 的 semantic.match 并存）。

    当 Planner 未在 semantic_metrics 声明指标时，程序日志不能只显示 tier=rag：
    这里按工具实际命中的指标记录 semantic.match，便于审计"查数实际走的语义层路径"。
    """
    ordered = []
    for _m in (metric_hits if metric_hits else []):
        if not _m:
            continue
        _copy = dict(_m)
        if metric_source == "metric_id":
            # Agent 明确指定指标 id：视为最高置信命中（缺失 confidence 时补 1.0）
            _copy.setdefault("confidence", 1.0)
            _copy.setdefault("score", _copy.get("grep_score", 1.0))
        ordered.append(_copy)
    ordered.sort(key=lambda m: float(m.get("confidence") or 0), reverse=True)
    confidences = [float(m.get("confidence") or 0) for m in ordered]
    top_conf = max(confidences, default=0.0)
    unique = (
        len(ordered) == 1 and top_conf >= SEMANTIC_CONFIDENCE_UNIQUE
    ) or (
        len(ordered) >= 2
        and top_conf >= SEMANTIC_CONFIDENCE_UNIQUE
        and (confidences[0] - confidences[1]) >= SEMANTIC_UNIQUE_GAP_THRESHOLD
    )
    tier = (
        "unique" if unique
        else ("candidate" if top_conf >= SEMANTIC_CONFIDENCE_CANDIDATE else "rag")
    )
    log_metric_event(
        "semantic.match",
        node_name="execute_query",
        source="execute_query_tool",
        metric_source=metric_source,
        mention=str(question or "")[:100],
        hit_count=len(ordered),
        metric_ids=[m.get("id", "") for m in ordered],
        metric_names=[m.get("name", "") for m in ordered],
        metric_scores=[m.get("grep_score", m.get("score", 0)) for m in ordered],
        metric_confidences=[round(c, 2) for c in confidences],
        top_confidence=round(top_conf, 2),
        semantic_unique=unique,
        tier=tier,
    )
    return ordered


def _build_result_summary(result_state: dict) -> str:
    """把执行链返回状态组装成给 Agent 的结果摘要（成功预览 / 0 行 / 失败原因）。"""
    plan_error = str(result_state.get("seeker_plan_error") or "")
    if plan_error:
        return f"查询方案不可行：{plan_error}"
    if result_state.get("sql_exec_failed"):
        return f"查询执行失败：{result_state.get('sql_exec_error') or '未知错误'}"
    sql_result = result_state.get("sql_result") or {}
    columns = list(sql_result.get("columns") or [])
    row_count = int(sql_result.get("row_count") or 0)
    preview = result_state.get("result_preview") or []
    result_id = str(result_state.get("result_id") or "")
    full_csv = str(result_state.get("result_csv") or "")
    lines = [f"查询成功：共 {row_count} 行，列：{', '.join(columns) or '无'}"]
    if preview:
        _header = "| " + " | ".join(columns) + " |"
        _sep = "| " + " | ".join(["---"] * len(columns)) + " |"
        lines.append(_header)
        lines.append(_sep)
        for _row in preview:
            lines.append("| " + " | ".join(str(_row.get(c, "")) for c in columns) + " |")
    if result_id:
        lines.append(f"结果引用：{result_id}")
    if full_csv:
        lines.append(f"全量 CSV：{full_csv}")
    if row_count == 0:
        lines.append("注意：本次查询返回 0 行，可能是过滤值与实际存储值不一致或确实无匹配数据。")
    return "\n".join(lines)


def build_execute_query_tool(runtime, seeker_graph):
    """构建查数工具：持有编译好的 Seeker 子图，内部同步执行并返回结果摘要。"""
    semantic_provider = runtime["semantic_metadata_provider"]

    @tool
    def execute_query(question: str, metric_id: str = "", filters: str = "", dimensions: str = "") -> str:
        """执行一次数据查询并返回结果摘要（列 + 预览行 + 行数 + 全量 CSV 路径）。

        需要查数时调用；多指标可分多次调用（并行预留）。参数：
        - question：查询意图（如"查询徐州大区今年同意返厂的返厂明细"）
        - metric_id：语义层指标 id（可选，先用 search_semantic 确认后传入更准）
        - filters：过滤条件（如"region_name='徐州大区' AND status='同意返厂'"），时间用 yyyy-MM-dd 日期区间
        - dimensions：需要展示/分组的维度（逗号分隔，可选）
        返回 0 行时请自行判断是过滤值问题（可 probe_values 探查）还是确实无数据。
        """
        # 1. 指标定位：优先用 Agent 声明的 metric_id，否则按问题词 grep 兜底
        metric_source = "metric_id"
        metric_hits = []
        if metric_id:
            _m = semantic_provider.get_metric_by_id(metric_id)
            if _m:
                metric_hits = [_m]
        if not metric_hits:
            metric_source = "grep_fallback"
            metric_hits = grep_metrics_from_keywords(
                _split_keywords(question),
                provider=semantic_provider.semantic_layer,
                limit=3,
            )
        if not metric_hits:
            return "未匹配到语义层指标，无法构建查询方案。可先用 search_semantic 检索指标，或补充指标/口径信息后再试。"
        # 记录工具内部真实语义层命中（供日志审计"实际走的语义层路径"）
        metric_hits = _log_metric_hit(metric_hits, question, metric_source)
        _top_metric = metric_hits[0]
        _hit_line = (
            f"已按语义层指标「{_top_metric.get('name', '')}」"
            f"（id={_top_metric.get('id', '')}）执行查询。"
        )
        # 2. 语义层确定性方案构建（字段由语义层权威决定，避免 LLM 猜字段）
        _dims = [d.strip() for d in str(dimensions or "").split(",") if d.strip()]
        plan = build_plan_from_semantic(
            metric_hits=metric_hits,
            semantic_provider=semantic_provider,
            dimension_mentions=_dims or None,
            filters=filters,
        )
        if plan is None:
            return "语义层方案构建失败（指标口径不完整或字段无法映射），无法执行查询。"
        # 3. 构造 Seeker 子图初始状态并同步执行（request 上下文由 planner 循环注入）
        state = {
            "confirmed_plan": plan,
            "request_id": _current_request_id.get(),
            "conversation_id": _current_conversation_id.get(),
            "topic_id": _current_topic_id.get(),
            "effective_query": question,
            "current_user_input": question,
        }
        try:
            result_state = seeker_graph.invoke(state)
        except Exception as error:
            return f"{_hit_line}\n查询执行异常：{error}"
        # 4. 组装结果摘要回填给 Agent（前置语义层命中行，供审计）
        return _hit_line + "\n" + _build_result_summary(result_state)

    return execute_query
