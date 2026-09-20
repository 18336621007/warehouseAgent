# execute_query_tool.py —— 查数工具：把 Seeker 执行链封装为一次工具调用
# 单 Agent（Codex 模式）下，Agent 在 ReAct 循环里调 execute_query 查数，
# 结果（列 + 预览 + 行数 + CSV 路径）回填上下文，Agent 直接基于结果写回答；
# 不再需要"执行后回 Planner 写回答"的第二次决策。
# 多数据源/并行：内部按 confirmed_plan 的 engine 取数据源（当前默认 hive），
# steps 多段查询内部并行执行（出错自动降级串行）；Agent 多 tool_calls 仍按顺序逐个执行。
import re
import json
from contextvars import ContextVar

from langchain.tools import tool
from agentTest.semantic_layer.metric_matcher import grep_metric_files_from_keywords
from agentTest.langgraph_app.services.plan_synthesizer import build_plan_from_semantic
from agentTest.langgraph_app.runtime.graph_logger import log_metric_event
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
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


def _precheck_filter_fields(filters: str, metric_hits: list, semantic_provider) -> tuple:
    """预校验 filters 字段能否在指标来源表/关联表内定位，返回 (无法定位字段, 来源表列表)。

    与 build_plan_from_semantic 的 scope 对齐（来源表 + join contracts 一跳可达表），
    提前拦截模型臆造字段，把具体失败信息（哪个字段、表时间分区字段）交给模型探查修正。
    """
    from agentTest.langgraph_app.services.plan_synthesizer import (
        _extract_filter_fields,
        _find_field_table,
    )
    fields = _extract_filter_fields(filters)
    if not fields:
        return [], []
    sl = semantic_provider.semantic_layer
    tables = set()
    for _m in (metric_hits or []):
        _src = str(_m.get("source_model") or "")
        if _src:
            tables.add(_src)
    scope_ids = set(tables)
    for _mid in list(scope_ids):
        for _c in sl.get_join_contracts_for_model(_mid):
            scope_ids.add(str(_c.get("left_model") or ""))
            scope_ids.add(str(_c.get("right_model") or ""))
    unmapped = [f for f in fields if not _find_field_table(f, scope_ids, sl)]
    return unmapped, sorted(tables)


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


def _parse_steps(steps) -> list:
    """把 steps 参数解析为子查询列表：接受 JSON 数组字符串或 list，非法返回空列表。"""
    if isinstance(steps, (list, tuple)):
        return [s for s in steps if isinstance(s, dict)]
    try:
        data = json.loads(str(steps or "").strip())
    except Exception:
        return []
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def _parse_step_metric_ids(step: dict) -> list[str]:
    """从 step 解析指标 id 列表：兼容 metric_ids（逗号分隔/JSON 数组）与 metric_id。"""
    ids = []
    _raw = step.get("metric_ids")
    if _raw:
        if isinstance(_raw, (list, tuple)):
            ids = [str(x) for x in _raw if str(x).strip()]
        else:
            _text = str(_raw).strip()
            if _text.startswith("["):
                try:
                    ids = [str(x) for x in json.loads(_text) if str(x).strip()]
                except Exception:
                    ids = [x.strip() for x in _text.strip("[]").split(",")]
            else:
                ids = [x.strip() for x in _text.split(",")]
    if not ids and step.get("metric_id"):
        ids = [str(step.get("metric_id"))]
    return [x for x in ids if x]


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

    def _run_single(question, metric_ids, filters, dimensions, dimension, request_id):
        """执行单个子查询：语义层定位 → 方案构建 → Seeker 子图 → 结果摘要。

        返回 (result_state, text)：result_state 供脚本元数据记录 SQL/引用，text 回填给 Agent。
        metric_ids 为显式指标 id 列表（同表多指标合并一条 SQL）；为空时按问题词 grep 兜底取 top1。
        dimension 为 dimensional_measures 子口径（如"激活电柜"），用于解析 {field} 占位符。
        """
        # 1. 指标定位：优先用 Agent 声明的 metric_ids，否则按问题词 grep 兜底（只取 top1）
        metric_source = "metric_id"
        metric_hits = []
        for _mid in (metric_ids or []):
            _m = semantic_provider.get_metric_by_id(_mid)
            if _m and _m not in metric_hits:
                metric_hits.append(_m)
        if not metric_hits:
            metric_source = "grep_fallback"
            metric_hits = grep_metric_files_from_keywords(
                _split_keywords(question),
                provider=semantic_provider.semantic_layer,
                limit=3,
            )[:1]
        if not metric_hits:
            return None, "未匹配到语义层指标，无法构建查询方案。可先用 grep_semantic/read_metric 定位指标，或补充指标/口径信息后再试。"
        # 记录工具内部真实语义层命中（供日志审计"实际走的语义层路径"）
        metric_hits = _log_metric_hit(metric_hits, question, metric_source)
        # 命中行同时带 name 与 id，便于 LLM/日志审计实际走的指标（id 为稳定标识）
        _hit_parts = []
        for _m in metric_hits:
            _n = str(_m.get("name") or _m.get("id") or "")
            _i = str(_m.get("id") or "")
            _hit_parts.append(f"{_n}（id={_i}）" if _i and _i != _n else _n)
        _hit_line = "已按语义层指标「" + "、".join(_hit_parts) + "」执行查询。"
        # 2.0 filters 字段预校验：拦截模型臆造字段，失败信息具体化（哪个字段、表时间分区字段）
        _unmapped_filters, _src_tables = _precheck_filter_fields(
            filters, metric_hits, semantic_provider
        )
        if _unmapped_filters:
            _tables_str = "、".join(_src_tables) or "相关表"
            # 收集来源表时间分区字段，作为修正时间过滤条件的提示
            _time_hints = []
            for _t in _src_tables:
                _tf = semantic_provider.get_table_time_field(_t)
                if _tf and _tf not in _time_hints:
                    _time_hints.append(_tf)
            _time_str = ("；若为时间过滤，该表时间分区字段为 " + "、".join(_time_hints)) if _time_hints else ""
            _bad_fields = "、".join(f"「{_f}」" for _f in _unmapped_filters)
            return None, (
                f"过滤字段 {_bad_fields} 无法在指标来源表及关联表（{_tables_str}）中定位{_time_str}。"
                "请用 search_columns 查询相关表确认真实字段名后修正 filters 重试。"
            )
        # 2. 语义层确定性方案构建（字段由语义层权威决定，避免 LLM 猜字段）
        # dimension（dimensional_measures 子口径）并入维度提及，供 {field} 占位符解析
        _dims = [d.strip() for d in str(dimensions or "").split(",") if d.strip()]
        if str(dimension or "").strip():
            _dims.append(str(dimension).strip())
        plan = build_plan_from_semantic(
            metric_hits=metric_hits,
            semantic_provider=semantic_provider,
            dimension_mentions=_dims or None,
            filters=filters,
        )
        if plan is None:
            return None, "语义层方案构建失败（指标口径不完整或字段无法映射），无法执行查询。"
        # 维度词未全部映射到物理字段：不静默退化为无分组聚合，提示 Planner 核实后重试
        _unresolved = plan.get("unresolved_dimensions") or []
        if _unresolved:
            _tables_hint = "、".join(str(t) for t in (plan.get("tables") or [])) or "相关表"
            return None, (
                "以下维度词未能映射到物理字段，无法按该维度分组："
                + "、".join(str(w) for w in _unresolved)
                + f"。涉及表：{_tables_hint}。请用 search_columns 查询相关表的真实字段名后重试；"
                + "若这些词实为过滤条件，请放入 filters。"
            )
        # 3. 构造 Seeker 子图初始状态并同步执行（request 上下文由 planner 循环注入）
        state = {
            "confirmed_plan": plan,
            "request_id": request_id,
            "conversation_id": _current_conversation_id.get(),
            "topic_id": _current_topic_id.get(),
            "effective_query": question,
            "current_user_input": question,
        }
        try:
            result_state = seeker_graph.invoke(state)
        except Exception as error:
            return None, f"{_hit_line}\n查询执行异常：{error}"
        # 4. 组装结果摘要回填给 Agent（前置语义层命中行，供审计）
        return result_state, _hit_line + "\n" + _build_result_summary(result_state)

    def _group_metric_ids_by_source(metric_ids):
        """把指标 id 列表按来源表分组（保持原顺序，同表合并），返回 [(ids, source_model), ...]。"""
        groups = []
        seen = {}
        for _mid in metric_ids:
            _m = semantic_provider.get_metric_by_id(_mid)
            if not _m:
                continue
            _src = str(_m.get("source_model") or "")
            if _src not in seen:
                seen[_src] = len(groups)
                groups.append(([], _src))
            groups[seen[_src]][0].append(_mid)
        return groups

    def _run_steps(steps_list, fallback_question, base_request_id):
        """并行执行多段查询：每段按来源表自动拆分（同表多指标合并一条 SQL），
        每组独立落盘（唯一 result_id）；不同段并行执行，出错逐级降级并行度（直到串行）。

        仿 Codex 查询脚本：一次提交多段查询，并行/串行执行、逐段保存原始结果，减少 LLM 工具往返。
        """
        from concurrent.futures import ThreadPoolExecutor
        from contextvars import copy_context

        from agentTest.config.planner import MAX_QUERY_PARALLEL
        from agentTest.langgraph_app.services.result_store import save_query_script

        # 1. 收集执行单元（保持原始顺序；同表多指标合并一条 SQL，异表拆成独立单元并行）
        units = []
        for _idx, _step in enumerate(steps_list):
            # step_id 只保留字母数字下划线：它拼入 request_id 并用于落盘文件名（Windows 不允许冒号等字符）
            _step_id = re.sub(r"[^0-9A-Za-z_]", "_", str(_step.get("id") or f"s{_idx + 1}")) or f"s{_idx + 1}"
            _s_question = str(_step.get("question") or fallback_question or "")
            _s_metric_ids = _parse_step_metric_ids(_step)
            _s_filters = str(_step.get("filters") or "")
            _s_dims = str(_step.get("dimensions") or "")
            _s_dimension = str(_step.get("dimension") or "")
            # 按来源表分组：同表多指标合并一条 SQL，异表自动拆组并行执行
            _groups = _group_metric_ids_by_source(_s_metric_ids)
            if not _groups:
                _groups = [([], "")]
            for _gi, (_ids, _src) in enumerate(_groups):
                # 每组独立 request_id，保证落盘 result_id / CSV 文件名唯一
                _sub_request_id = (
                    f"{base_request_id}_{_step_id}_g{_gi + 1}"
                    if len(_groups) > 1
                    else f"{base_request_id}_{_step_id}"
                )
                units.append({
                    "key": _sub_request_id,
                    "step_id": _step_id if len(_groups) == 1 else f"{_step_id}_g{_gi + 1}",
                    "question": _s_question,
                    "metric_ids": list(_ids),
                    "source_model": _src,
                    "filters": _s_filters,
                    "dimensions": _s_dims,
                    "dimension": _s_dimension,
                    "request_id": _sub_request_id,
                })

        def _exec_unit(_u):
            # 用 copy_context 把主线程的日志/会话 ContextVar 传播进工作线程，保证日志归属与落盘正确
            _ctx = copy_context()
            return _ctx.run(
                _run_single, _u["question"], _u["metric_ids"], _u["filters"],
                _u["dimensions"], _u["dimension"], _u["request_id"],
            )

        def _is_failed(_rs):
            # 失败判定：方案不可行 / 执行失败 / 返回 None
            if _rs is None:
                return True
            return bool(_rs.get("seeker_plan_error") or _rs.get("sql_exec_failed"))

        # 2. 并行执行，出错逐级降级并行度（如 4→2→1），降级后仅重跑失败单元
        results = {}
        failed_keys = [_u["key"] for _u in units]
        _parallel = MAX_QUERY_PARALLEL
        while failed_keys:
            _todo = [_u for _u in units if _u["key"] in failed_keys]
            if len(_todo) == 1:
                _parallel = 1
            if _parallel <= 1:
                # 串行兜底：逐个执行（保证顺序与上下文稳定）
                for _u in _todo:
                    results[_u["key"]] = _exec_unit(_u)
                break
            with ThreadPoolExecutor(max_workers=_parallel) as _ex:
                _futs = {_ex.submit(_exec_unit, _u): _u for _u in _todo}
                for _fut, _u in _futs.items():
                    try:
                        results[_u["key"]] = _fut.result()
                    except Exception as _err:
                        results[_u["key"]] = (None, f"查询执行异常：{_err}")
            _next_failed = [_k for _k, (_rs, _txt) in results.items() if _is_failed(_rs)]
            if not _next_failed:
                break
            # 并行度减半重试失败单元（可能是并发压力导致，降级后重试）
            _half = _parallel // 2
            if _half < 1:
                break
            log_sub_info(
                f"多段查询并行执行有 {len(_next_failed)} 段失败，降级并行度 {_parallel} -> {_half} 重试",
                node_name="execute_query",
            )
            _parallel = _half
            failed_keys = _next_failed

        # 3. 按原始顺序组装摘要与脚本元数据
        summaries = []
        step_infos = []
        for _u in units:
            _result_state, _text = results[_u["key"]]
            _sql_result = (_result_state or {}).get("sql_result") or {}
            _last_result = (_result_state or {}).get("last_query_result") or {}
            step_infos.append({
                "step_id": _u["step_id"],
                "question": _u["question"],
                "metric_ids": list(_u["metric_ids"]),
                "source_model": _u["source_model"],
                "filters": _u["filters"],
                "dimensions": _u["dimensions"],
                "dimension": _u["dimension"],
                "generated_sql": str((_result_state or {}).get("generated_sql") or ""),
                "result_id": str((_result_state or {}).get("result_id") or ""),
                "round_no": _last_result.get("round_no"),
                "row_count": int(_sql_result.get("row_count") or 0),
                "columns": list(_sql_result.get("columns") or []),
                "full_csv": str((_result_state or {}).get("result_csv") or ""),
            })
            summaries.append(f"[{_u['step_id']}] {_text}")
        # 保存查询脚本元数据（仿 Codex），供审计与后续按段引用中间结果
        save_query_script(_current_conversation_id.get(), base_request_id, step_infos)
        return (
            "已并行执行多段查询（每段结果已落盘，可 query_stored_result 按 result_id/round_no 引用）：\n\n"
            + "\n\n".join(summaries)
        )

    @tool
    def execute_query(question: str, metric_id: str = "", filters: str = "", dimensions: str = "", dimension: str = "", steps: str = "") -> str:
        """执行一次数据查询并返回结果摘要（列 + 预览行 + 行数 + 全量 CSV 路径）。

        需要查数时调用；多指标可用 steps 一次提交多段查询（并行执行、每段结果独立落盘，仿 Codex 查询脚本）。参数：
        - question：查询意图（如"查询徐州大区今年同意返厂的返厂明细"）
        - metric_id：语义层指标 id（可选，先用 grep_semantic/read_metric 确认后传入更准）
        - filters：过滤条件（如"region_name='徐州大区' AND status='同意返厂'"），时间用 yyyy-MM-dd 日期区间
        - dimensions：需要展示/分组的维度（逗号分隔，可选）
        - dimension：指标的可选子口径，填与用户问法一致的口径名称（如"激活电柜"），不是物理字段名；拿不准时先 search_columns 确认可选口径再决定执行
        - steps：可选，多段查询脚本的 JSON 数组字符串，每段 {id, question, metric_ids, metric_id, filters, dimensions, dimension}；
          同一来源表、同一时间/过滤条件的多个指标用 metric_ids 合并到一个 step，程序按表自动合并为一条 SQL（多列聚合）；
          不同来源表自动拆组并行执行（每组独立 result_id）。非空时并行执行每段并各自落盘，出错自动降级串行，返回各段摘要与结果引用
        返回 0 行时请自行判断是过滤值问题（可 probe_values 探查）还是确实无数据。
        """
        base_request_id = _current_request_id.get()
        if str(steps or "").strip():
            steps_list = _parse_steps(steps)
            if not steps_list:
                return (
                    "steps 解析失败：需要 JSON 数组字符串，如 "
                    "[{\"id\":\"s1\",\"question\":\"昨天新增订单数\",\"metric_id\":\"addition_order_num\","
                    "\"filters\":\"\",\"dimensions\":\"\"}]"
                )
            return _run_steps(steps_list, question, base_request_id)
        result_state, text = _run_single(
            question, [metric_id] if metric_id else [], filters, dimensions, dimension, base_request_id
        )
        return text

    return execute_query

