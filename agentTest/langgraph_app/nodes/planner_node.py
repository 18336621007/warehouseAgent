# Planner 调度节点（单 Agent / Codex 模式）：轻量 query 改写 + ReAct 工具循环内查数写回答
#
# Planner 是唯一 Agent：查数通过 execute_query 工具在循环内完成，route 收敛为 respond 单一终态（直接输出文本）。
# 流程：
#   ① 轻量 query 改写（fast 独立小调用）：结合本轮输入与对话历史还原完整有效需求 effective_query
#   ② Planner ReAct 工具循环（grep_semantic/read_metric/list_metric_files/search_tables/search_columns/probe_values/query_stored_result/execute_query）
#   ③ 模型主动停止工具时，自由文本即最终回答；查数结果回填后直接写回答，route 一律 respond 结束本轮
import json
import re
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from agentTest.langgraph_app.services.thinking_stream_chat import ThinkingStreamChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage
from agentTest.langgraph_app.services.query_plan_service import (
    _extract_time_from_filters,
    lock_query_plan,
)
from agentTest.langgraph_app.services.result_store import list_result_index
from agentTest.config.settings import (
    get_openai_api_key,
    get_openai_base_url,
    get_model_name,
    get_model_extra_body,
    get_llm_fast_model,
    get_model_context_window,
    get_skill_index_max_chars,
)
from agentTest.langgraph_app.prompts.planner_prompt import (
    PLANNER_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
)
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_example_retrieved
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
from agentTest.langgraph_app.runtime.graph_logger import log_tools_called
from agentTest.langgraph_app.runtime.graph_logger import log_skill_event
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.langgraph_app.runtime.llm_log_handler import build_llm_logging_handler
from agentTest.langgraph_app.tools.result_query_tool import (
    set_result_conversation,
    reset_result_conversation,
)
from agentTest.langgraph_app.tools.execute_query_tool import (
    set_execute_query_context,
    reset_execute_query_context,
)
from agentTest.langgraph_app.tools.semantic_tool import (
    begin_semantic_dedup,
    end_semantic_dedup,
    get_semantic_dedup_summary,
    set_semantic_render_budget,
    reset_semantic_render_budget,
)
from agentTest.langgraph_app.runtime.stream_bus import get_stream_bus
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.config.planner import (
    MAX_PLANNER_TOOL_STEPS,
    MAX_LLM_RETRY,
    CONTEXT_BUDGET_OUTPUT_RESERVE_RATIO,
    TOOL_RESULT_MAX_BUDGET_RATIO,
    TOOL_RESULT_MIN_CHARS,
    TOOL_RESULT_MAX_CHARS,
    CONTEXT_COMPACT_REDLINE_RATIO,
    PLANNER_CONTEXT_KEEP_ROUNDS_MAX,
    PLANNER_CONTEXT_KEEP_ROUNDS_MIN,
    CHARS_PER_TOKEN_ESTIMATE,
    MAX_QUERY_PARALLEL,
)
from openai import APIError


def _build_history_context(messages, max_turns=10, max_chars_per_msg=500, exclude_user_id=""):
    """把最近几轮用户消息与最终回答组装成对话历史，过滤工具消息与 ReAct 中间步骤。
    exclude_user_id：本轮输入的 user 消息 id（{request_id}:user），用于排除本轮、只保留真正历史。"""
    from langchain_core.messages import ToolMessage, AIMessage
    lines = []
    for msg in (messages or [])[-max_turns * 2:]:
        name = getattr(msg, "name", "") or ""
        msg_id = str(getattr(msg, "id", "") or "")
        if exclude_user_id and msg_id == exclude_user_id:
            # 本轮输入不计入历史（首轮无历史，避免"对话历史=本轮输入"重复）
            continue
        if isinstance(msg, HumanMessage):
            role = "用户"
        elif isinstance(msg, ToolMessage):
            # 工具结果不属于对话历史，跳过
            continue
        elif isinstance(msg, AIMessage):
            # 只保留最终回答（id 以 :advisor/:seeker 结尾且无 tool_calls），
            # 过滤 ReAct 中间步骤（含 tool_calls 或纯文本思考）
            if getattr(msg, "tool_calls", None):
                continue
            if not (msg_id.endswith(":respond") or msg_id.endswith(":seeker") or msg_id.endswith(":advisor")):
                continue
            role = f"助手({name})" if name else "助手"
        else:
            continue
        content = str(msg.content or "")
        if len(content) > max_chars_per_msg:
            # 截断时提示内容可能不全：数据类回答可调 query_stored_result 读全量，避免凭残片推断
            if isinstance(msg, AIMessage):
                content = (
                    content[:max_chars_per_msg]
                    + "……（该条历史回答较长已截断，如需其中字段的完整取值，可调用 query_stored_result 读取对应落盘结果）"
                )
            else:
                content = content[:max_chars_per_msg] + "……（该条历史回答较长已截断）"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _build_minimal_plan(planner_output) -> dict | None:
    """Planner 判定 seeker 但语义层未命中时，用 Planner 输出构造最小方案。

    聚合表达式（count(*)/sum(x)）与时间字段不作为分组维度，
    聚合由 generate_sql 的 LLM 根据 effective_query 生成。"""
    tables = list(planner_output.tables or [])
    fields = list(planner_output.fields or [])
    filters = planner_output.filters or ""
    analysis_type = planner_output.analysis_type or ""
    if not tables:
        return None
    # 清洗聚合表达式（不是物理字段，由 LLM 负责聚合）；聚合时时间字段不参与分组
    is_detail = str(analysis_type).lower() in ("detail", "detail_query")
    clean_fields = [
        f for f in fields
        if re.fullmatch(r"[A-Za-z_]+\([^)]*\)", str(f).strip()) is None
    ]
    time_field, time_range = _extract_time_from_filters(filters)
    if time_field and not is_detail:
        clean_fields = [f for f in clean_fields if f != time_field]
    # 非明细聚合查询：filters 未显式给出时间条件时回退默认分区字段 pt_dt；
    # 明细查询（可能为无分区明细表）必须由 filters 明确业务时间字段，禁止回退。
    if not time_field and not is_detail:
        time_field = "pt_dt"
    plan = {
        "table": tables[0],
        "tables": tables,
        "select_fields": clean_fields,
        "filters": filters,
        "detail_query": is_detail,
        "time_field": time_field,
        "time_range": time_range or "昨天",
        "result_limit": 1000,
    }
    try:
        return lock_query_plan(plan)
    except Exception:
        return None


def _format_result_index(result_index: list) -> str:
    """把结果历史索引格式化成轻量文本（只含摘要，供 LLM 指代历史结果轮次）。
    含引用标识 result_id 与 CSV 路径，供 LLM 决定是否基于落盘结果回答。"""
    lines = []
    for entry in result_index:
        round_no = entry.get("round_no", "")
        created_at = str(entry.get("created_at") or "")[11:16]
        query = str(entry.get("effective_query") or "")[:80]
        row_count = entry.get("row_count", 0)
        # 列出该轮全部列名（只含列名不含数据），便于 LLM 判断可对哪些字段做统计/过滤
        columns = ", ".join(entry.get("columns") or [])
        line = f"- 第{round_no}轮 ({created_at}): {query} | {row_count}行 | 列: {columns}"
        result_id = entry.get("result_id", "")
        # 优先用绝对路径（result_store 新增 full_csv_path），历史轮次也能给出完整保存路径
        full_csv = entry.get("full_csv_path") or entry.get("full_csv", "")
        if result_id:
            line += f" | 引用: {result_id}"
        if full_csv:
            line += f" | CSV: {full_csv}"
        entity_keys = entry.get("entity_keys") or []
        if entity_keys:
            line += f" | 实体键: {', '.join(str(k) for k in entity_keys[:5])}"
        lines.append(line)
    return "\n".join(lines)

def _emit_context_progress(response):
    """每轮工具调用后推送上下文使用进度（prompt tokens / 窗口），供前端展示进度条。"""
    bus = get_stream_bus()
    if bus is None:
        return
    usage = getattr(response, "usage_metadata", None) or {}
    used = int(usage.get("input_tokens") or 0)
    if not used:
        # 回退：自定义 ChatModel 走 _generate（非 _stream）时，token 位于 response_metadata.token_usage
        meta = getattr(response, "response_metadata", None) or {}
        token_usage = meta.get("token_usage") or {}
        used = int(token_usage.get("prompt_tokens") or 0)
    if not used:
        return
    window = get_model_context_window()
    bus.emit({
        "type": "context_progress",
        "used_tokens": used,
        "window_tokens": window,
        "percent": round(min(100.0, used * 100.0 / window), 1),
    })


def _emit_context_compacted(before_chars, after_chars):
    """上下文压缩发生时推送提示，供前端展示"上下文已压缩"。"""
    bus = get_stream_bus()
    if bus is None:
        return
    bus.emit({
        "type": "context_compacted",
        "before_chars": before_chars,
        "after_chars": after_chars,
        "saved_chars": max(0, before_chars - after_chars),
    })


# 工具名 → 前端步骤文案：思考面板展示真实执行步骤（对齐 Codex），不暴露工具内部实现细节
_TOOL_LABELS = {
    "grep_semantic": "正在检索语义层指标...",
    "read_metric": "正在读取指标口径...",
    "list_metric_files": "正在浏览语义层指标清单...",
    "search_databases": "正在检索数据库...",
    "search_tables": "正在检索数据表...",
    "search_columns": "正在检索字段...",
    "query_stored_result": "正在读取历史查询结果...",
    "probe_values": "正在探查字段实际值...",
    "execute_query": "正在执行查询...",
    "read_skill": "正在读取技能说明...",
}


def _emit_tool_event(tool_name: str) -> None:
    """工具实际执行前推送一步事件到前端，让思考面板展示真实工具步骤。"""
    bus = get_stream_bus()
    if bus is None:
        return
    label = _TOOL_LABELS.get(tool_name)
    if not label:
        return
    bus.emit({"type": "thinking", "node": tool_name, "text": label})


def _estimate_tokens(text) -> int:
    """按中文字符/token 系数估算文本 token 占用（偏保守高估，预留安全余量）。"""
    return int(len(str(text)) / CHARS_PER_TOKEN_ESTIMATE)


def _get_context_budget(used_tokens) -> int:
    """按剩余上下文预算计算可用 token 额度（对齐 Codex 动态收敛）。
    剩余预算 = 窗口 − 已用 token − 输出预留；预算不足返回 0（触发压缩兜底）。
    """
    window = get_model_context_window()
    reserve = int(window * CONTEXT_BUDGET_OUTPUT_RESERVE_RATIO)
    return max(0, window - int(used_tokens or 0) - reserve)


def _maybe_compact_react_messages(messages, used_tokens=0, added_chars=0, keep_rounds=None):
    """触发式上下文压缩（通用，不区分场景）：预计下一轮输入将超过窗口预算红线时，
    把早期工具轮替换为【已获取信息摘要】，保留 System/Human 与最近 keep_rounds 轮完整内容；
    触发条件与压缩强度均由剩余上下文预算动态派生（对齐 Codex），未超红线返回原列表（零开销）。
    """
    window = get_model_context_window()
    # 估算下一轮输入 token = 当前真实已用 + 本轮新增结果的估算增量
    estimated_next = int(used_tokens or 0) + _estimate_tokens(added_chars)
    redline = int(window * CONTEXT_COMPACT_REDLINE_RATIO)
    if estimated_next <= redline:
        return messages
    # 压缩强度随剩余预算动态：预算越紧，保留的最近完整工具轮数越少（MAX→MIN 连续映射）
    budget = _get_context_budget(used_tokens)
    budget_ratio = budget / max(1, window)
    if keep_rounds is None:
        keep_rounds = max(
            PLANNER_CONTEXT_KEEP_ROUNDS_MIN,
            int(round(PLANNER_CONTEXT_KEEP_ROUNDS_MAX * budget_ratio)),
        )
    total_chars = sum(len(str(m.content or "")) for m in messages)
    summary = get_semantic_dedup_summary()
    # 消息头：System(+skill System) + Human 需求基线，始终保留
    head_end = 1
    if len(messages) > 1 and isinstance(messages[1], SystemMessage):
        head_end = 2
    if len(messages) > head_end and isinstance(messages[head_end], HumanMessage):
        head_end += 1
    # 尾部：保留最近 keep_rounds 对（AI+Tool）及最后一条无工具调用的 AI（停止信号）
    tail = []
    idx = len(messages) - 1
    pairs = 0
    stop_seen = False
    while idx > head_end and pairs < keep_rounds:
        if isinstance(messages[idx], ToolMessage):
            ai_idx = idx
            while ai_idx > head_end and not isinstance(messages[ai_idx], AIMessage):
                ai_idx -= 1
            if ai_idx <= head_end:
                break
            tail = messages[ai_idx:idx + 1] + tail
            idx = ai_idx - 1
            pairs += 1
        elif isinstance(messages[idx], AIMessage) and not getattr(messages[idx], "tool_calls", None) and not stop_seen:
            # 停止信号 AI 保留，并继续向前收集最近的工具结果轮
            tail = [messages[idx]] + tail
            idx -= 1
            stop_seen = True
        else:
            idx -= 1
    compacted = list(messages[:head_end])
    if summary:
        compacted.append(SystemMessage(content=summary))
    compacted.extend(tail)
    _emit_context_compacted(total_chars, sum(len(str(m.content or "")) for m in compacted))
    return compacted


def _condense_skill_heading(text, max_lines=8):
    """提取技能正文的 markdown 标题骨架作为轻量摘要（保留章节结构，细节按需重新 read_skill）。"""
    out = []
    for ln in str(text or "").splitlines():
        if ln.strip().startswith("#"):
            out.append(ln.strip())
            if len(out) >= max_lines:
                break
    return "\n".join(out) if out else str(text or "")[:200]


def _condense_read_skill_results(messages, keep_tool_call_ids):
    """把非本轮 read_skill 结果替换为轻量摘要：技能全文只在读取那一轮保留，
    后续轮只需章节骨架（对齐 Codex auto-compact 早期轮收敛），细节可重新 read_skill。"""
    out = []
    for m in messages:
        if (isinstance(m, ToolMessage)
                and getattr(m, "name", "") == "read_skill"
                and m.tool_call_id not in keep_tool_call_ids):
            headings = _condense_skill_heading(m.content)
            out.append(ToolMessage(
                content=f"【技能已读】章节骨架：\n{headings}\n（全文已在上文提供，如需重看请再次调用 read_skill）",
                tool_call_id=m.tool_call_id,
                name="read_skill",
            ))
        else:
            out.append(m)
    return out


def _invoke_llm_with_retry(llm, messages, node_name="planner"):
    """LLM 调用瞬时错误重试：模型端 response_format JSON 偶发异常（APIError 400/5xx）时退避重试。

    invoke 抛异常时 messages 未被修改、无副作用（工具不会重复执行），重试幂等安全；
    仅对 openai.APIError 重试，其余异常直接上抛避免掩盖真实错误。
    """
    last_err = None
    for _attempt in range(MAX_LLM_RETRY + 1):
        try:
            if _attempt:
                log_sub_info(
                    f"LLM 调用第 {_attempt} 次重试（上次异常: {type(last_err).__name__}）",
                    node_name=node_name,
                )
            return llm.invoke(messages)
        except APIError as _err:
            last_err = _err
            if _attempt >= MAX_LLM_RETRY:
                break
            # 退避 0.5s 递增，避免瞬时故障时高频重试
            time.sleep(0.5 * (_attempt + 1))
    raise last_err


def build_planner_node(runtime):
    # M2：Planner 工具化，从统一注册表取只读安全工具（元数据检索 + 落盘结果预览）
    planner_tools = runtime["tool_registry"].get(group="planner")

    # 思考流式 ChatModel：底层用 OpenAI 兼容 SDK 流式调用，
    # 捕获 qwen thinking 的 reasoning_content 推送到前端思考面板（stream_bus），输出保持 langchain AIMessage
    chat_openai = ThinkingStreamChatModel(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        extra_body=get_model_extra_body(),
        callbacks=[build_llm_logging_handler("planner")],
        # 自由文本：增量 content 直接推前端回答区（对齐 Codex）
        stream_raw_content=True,
    )
    # 方案2：工具循环/快速定稿模型（与主模型同开思考，避免非思考误判路由）；模型名可用 LLM_FAST_MODEL 覆盖
    chat_openai_fast = ThinkingStreamChatModel(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_llm_fast_model(),
        extra_body=get_model_extra_body(),
        callbacks=[build_llm_logging_handler("planner_fast")],
        # 自由文本：增量 content 直接推前端回答区（对齐 Codex）
        stream_raw_content=True,
    )
    # 轻量 query 改写：fast 独立小调用（不流式到前端），结合本轮输入与对话历史
    # 还原完整有效需求 effective_query，供展示/落盘/execute_query 复用
    rewrite_llm = ThinkingStreamChatModel(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_llm_fast_model(),
        extra_body=get_model_extra_body(),
        callbacks=[build_llm_logging_handler("planner_rewrite")],
    )

    # M2：ReAct 工具循环 LLM，可自主调用 planner_tools 补充信息；信息足够后直接输出最终回答文本
    # 动作分层：工具调用轮用 fast 模型提速；fast 与主模型同开思考，保证判断质量
    react_llm = chat_openai_fast.bind_tools(planner_tools)

    def planner_node(state):
        # 每次调用只要求传入本轮输入
        current_user_input = state["current_user_input"]
        # 注入查数工具上下文（request/会话归属，工具内部日志与落盘用）
        _exec_ctx = set_execute_query_context(
            state.get("request_id", ""),
            state.get("conversation_id", ""),
            state.get("topic_id", ""),
        )
        # 开启语义层指标读取去重：read_metric 同一文件重复读时提示直接引用，不重复注入完整口径
        _sem_token = begin_semantic_dedup()

        # ── 去 Topic 化：不再向 prompt 注入【当前查询方案】（confirmed_context）──
        # Planner 完全依赖【完整对话历史】判断当前需求（历史含最终回答的方案信息），
        # 避免上一轮遗留方案干扰新需求理解；查询方案由 execute_query 工具按语义层构建

        # ── LLM 评估流程 ──
        timer = start_timer()
        log_node_start("planner", question=current_user_input)

        try:

            # ── 第0层：组装对话历史（排除本轮 user 消息，首轮应为空）──
            _history_user_id = f"{state.get('request_id', '')}:user"
            history_context = _build_history_context(
                state.get("messages") or [],
                exclude_user_id=_history_user_id,
            )

            # ── 轻量 query 改写（对齐 Codex）：结合本轮输入与对话历史还原完整有效需求 ──
            # 独立 fast 小调用产出 effective_query 字符串（供展示/落盘/execute_query 复用），
            # 改写失败不影响主流程，沿用本轮原始输入
            effective_query = current_user_input
            try:
                _rewrite_sections = [f"【当前日期】\n{date.today().isoformat()}"]
                if history_context:
                    _rewrite_sections.append(f"【对话历史（最近 N 轮）】\n{history_context}")
                _rewrite_sections.append(f"【本轮输入】\n{current_user_input}")
                _rewrite_resp = _invoke_llm_with_retry(
                    rewrite_llm,
                    [
                        SystemMessage(content=REWRITE_SYSTEM_PROMPT),
                        HumanMessage(content="\n\n".join(_rewrite_sections)),
                    ],
                )
                _rewrite_text = str(getattr(_rewrite_resp, "content", "") or "").strip()
                if _rewrite_text:
                    effective_query = _rewrite_text
            except Exception as _err:
                log_sub_info(f"query 改写失败，沿用原始输入: {type(_err).__name__}", node_name="planner")

            # ── 检索历史优质示例（仅对话首轮注入，避免历史相似问题干扰当前需求）──
            example_vs = runtime.get("example_vector_store")
            example_context = ""
            is_first_turn = len(state.get("messages") or []) <= 1
            if example_vs and is_first_turn:
                examples = example_vs.search_similar(current_user_input, k=2)
                if examples:
                    lines = []
                    for doc in examples:
                        q = doc.metadata.get("question", "")
                        lines.append(f"- {q}")
                    example_context = "\n".join(lines)
                    top_q = examples[0].metadata.get("question", "")[:50]
                    sim_val = examples[0].metadata.get("_similarity","?") if examples else "?"
                    log_example_retrieved(
                        "planner",
                        hit_count=len(examples),
                        top_sim=sim_val,
                        top_question=top_q,
                    )


            # ── 步骤②：组装用户消息并进入 ReAct 工具循环 ──
            # 组装用户消息 sections：有内容的才带标题，避免空标题占用 token
            # （元数据由工具自主检索，不再程序注入）
            sections = [
                f"【当前日期】\n{date.today().isoformat()}",
            ]
            if history_context:
                sections.append(f"【对话历史（最近 N 轮）】\n{history_context}")
            if example_context:
                sections.append(f"【历史相似问题】\n{example_context}")
            # 注入最近几轮查询结果索引（只含摘要），供模型识别"第三轮/刚才的结果"等指代
            result_index = list_result_index(state.get("conversation_id") or "", limit=8)
            if result_index:
                sections.append("【最近查询结果索引】\n" + _format_result_index(result_index))
            # 用户本轮输入（rewrite 改写后的完整需求 effective_query）放在最后，
            # 作为离模型决策最近的一条消息，对齐 Codex「最新一条用户消息即当前任务」的结构
            sections.append(f"【用户本轮输入】\n{effective_query}")
            user_content = "\n\n".join(sections)

            # ── M2：Planner ReAct 工具循环（自主决定是否补充检索/读落盘结果）──
            # skill 渐进式披露（仿 Codex）：只注入所有技能 name+description 索引，
            # 由 LLM 判断当前任务是否匹配某技能，匹配时自主调用 read_skill 读取完整正文
            skill_manager = runtime.get("skill_manager")
            skill_index = (
                skill_manager.list_skills_index(
                    scope="planner",
                    max_chars=get_skill_index_max_chars(),
                )
                if skill_manager else ""
            )
            if skill_index:
                log_skill_event(
                    "planner",
                    name=",".join(s.name for s in skill_manager.skills),
                    hit_count=len(skill_manager.skills),
                    mode="progressive_disclosure",
                )
            react_messages = [
                SystemMessage(content=PLANNER_SYSTEM_PROMPT),
            ]
            if skill_index:
                react_messages.append(SystemMessage(content=f"【可用技能】\n{skill_index}"))
            react_messages.append(HumanMessage(content=user_content))
            planner_tool_map = {t.name: t for t in planner_tools}
            # query_stored_result 依赖会话上下文：循环期间注入，结束后复位
            _conv_token = set_result_conversation(str(state.get("conversation_id") or ""))
            # 工具同参数去重集合：重复调用不再重复注入全量结果，防 prompt 膨胀
            _seen_tool_results = {}
            try:
                # 单程工具循环（对齐 Codex）：模型自主决定停止与回答，程序仅做步数保护
                _final_text = ""
                # 本轮真实 input_tokens：供工具结果收敛与压缩按剩余预算动态决策
                _current_input_tokens = 0
                for _step in range(MAX_PLANNER_TOOL_STEPS):
                    _response = _invoke_llm_with_retry(react_llm, react_messages)
                    # 每轮工具调用后推送上下文使用进度（prompt tokens / 窗口）
                    _emit_context_progress(_response)
                    # 记录本轮真实 input_tokens（usage_metadata），供预算计算与压缩触发
                    _usage = getattr(_response, "usage_metadata", None) or {}
                    _current_input_tokens = int(_usage.get("input_tokens") or 0)
                    if not _current_input_tokens:
                        # 回退：自定义 ChatModel 走 _generate 时 token 位于 response_metadata.token_usage（对齐 _emit_context_progress）
                        _meta = getattr(_response, "response_metadata", None) or {}
                        _token_usage = _meta.get("token_usage") or {}
                        _current_input_tokens = int(_token_usage.get("prompt_tokens") or 0)
                    react_messages.append(_response)
                    _tool_calls = getattr(_response, "tool_calls", None) or []
                    if not _tool_calls:
                        # 模型主动停止工具：该轮自由文本即最终回答（对齐 Codex），直接结束循环
                        _final_text = str(getattr(_response, "content", "") or "").strip()
                        break
                    log_tools_called("planner", [str(tc.get("name", "?")) for tc in _tool_calls])
                    # 同一轮多个 tool_calls 并行执行（execute_query/probe_values 等耗时工具提速，仿 Codex），
                    # 出错由 execute_query 内部逐级降级；结果按原始顺序回填，保证消息顺序稳定
                    def _invoke_tool(_tc, _idx, _tool):
                        try:
                            if _tc.get("name") == "execute_query":
                                # 并行执行时给每个 execute_query 唯一 request_id，避免落盘 result_id/CSV 冲突
                                _exec_ctx = set_execute_query_context(
                                    f"{state.get('request_id', '')}_p{_idx + 1}",
                                    str(state.get("conversation_id") or ""),
                                    str(state.get("topic_id") or ""),
                                )
                                try:
                                    return _tool.invoke(_tc.get("args") or {})
                                finally:
                                    reset_execute_query_context(_exec_ctx)
                            return _tool.invoke(_tc.get("args") or {})
                        except Exception as _err:
                            return f"工具调用失败: {_err}"

                    def _exec_tool_call(_tc, _idx, _tool):
                        # 推送工具执行事件到前端（实际执行的工具才推，去重跳过的不推）
                        _emit_tool_event(str(_tc.get("name") or ""))
                        # 用 copy_context 传播主线程的日志/会话 ContextVar，保证子线程日志归属正确
                        _ctx = copy_context()
                        return _ctx.run(_invoke_tool, _tc, _idx, _tool)

                    # 去重决策在主线程完成（避免并发竞争）；实际工具调用并行执行
                    _prepared = []
                    for _idx, _tc in enumerate(_tool_calls):
                        _tool = planner_tool_map.get(_tc.get("name"))
                        # 所有工具按同参数去重：重复调用不再重复注入全量结果，防 prompt 膨胀
                        _arg_key = json.dumps(_tc.get("args") or {}, ensure_ascii=False, sort_keys=True)
                        _dedup_key = f"{_tc.get('name')}|{_arg_key}"
                        if _tool is None:
                            _prepared.append((_tc, _idx, f"未知工具: {_tc.get('name')}"))
                        elif _dedup_key in _seen_tool_results:
                            _prepared.append((_tc, _idx, f"工具 {_tc.get('name')} 同参数已在上文返回，请直接引用上文结果，无需重复检索。"))
                        else:
                            _seen_tool_results[_dedup_key] = True
                            _prepared.append((_tc, _idx, None))
                    _todo = [(_tc, _idx) for _tc, _idx, _skip in _prepared if _skip is None]
                    # 按剩余上下文预算计算本轮工具结果配额（对齐 Codex 动态收敛，不写死字符数）
                    _budget_tokens = _get_context_budget(_current_input_tokens)
                    _tool_result_quota = max(
                        TOOL_RESULT_MIN_CHARS,
                        min(
                            TOOL_RESULT_MAX_CHARS,
                            int(_budget_tokens * TOOL_RESULT_MAX_BUDGET_RATIO * CHARS_PER_TOKEN_ESTIMATE),
                        ),
                    )
                    # 预算传给语义层检索工具：预算紧张时自动精简口径（完整口径由 execute_query 程序反查）
                    _sem_budget_token = set_semantic_render_budget(_tool_result_quota)
                    try:
                        if len(_todo) > 1:
                            _parallel = min(len(_todo), MAX_QUERY_PARALLEL)
                            with ThreadPoolExecutor(max_workers=_parallel) as _ex:
                                _futs = {
                                    _ex.submit(_exec_tool_call, _tc, _idx, planner_tool_map.get(_tc.get("name"))): (_tc, _idx)
                                    for _tc, _idx in _todo
                                }
                                _run_results = {}
                                for _fut, (_tc, _idx) in _futs.items():
                                    try:
                                        _run_results[_idx] = _fut.result()
                                    except Exception as _err:
                                        _run_results[_idx] = f"工具调用失败: {_err}"
                        else:
                            _run_results = {}
                            for _tc, _idx in _todo:
                                _run_results[_idx] = _exec_tool_call(_tc, _idx, planner_tool_map.get(_tc.get("name")))
                    finally:
                        reset_semantic_render_budget(_sem_budget_token)
                    _prev_react_chars = sum(len(str(m.content or "")) for m in react_messages)
                    _new_tool_call_ids = set()
                    for _tc, _idx, _skip in _prepared:
                        _result = _skip if _skip is not None else _run_results.get(_idx, "工具调用失败")
                        if _tc.get("name") == "read_skill":
                            # 模型显式要求读全文：不走预算收敛，仅受安全上限约束（name 标记供后续摘要化）
                            _max_result = TOOL_RESULT_MAX_CHARS
                            _new_tool_call_ids.add(_tc.get("id"))
                        else:
                            # 其余工具结果按剩余预算动态收敛（预算充足时全量保留）
                            _max_result = _tool_result_quota
                        react_messages.append(ToolMessage(
                            content=str(_result)[:_max_result],
                            tool_call_id=_tc.get("id"),
                            name=("read_skill" if _tc.get("name") == "read_skill" else None),
                        ))
                    # 技能全文只在读取那轮保留，后续轮收敛为章节骨架，避免技能正文常驻膨胀
                    react_messages = _condense_read_skill_results(react_messages, _new_tool_call_ids)
                    # 触发式上下文压缩：预计下一轮输入将超预算红线时压缩早期轮次（动态，对齐 Codex）
                    _added_chars = sum(len(str(m.content or "")) for m in react_messages) - _prev_react_chars
                    react_messages = _maybe_compact_react_messages(
                        react_messages, used_tokens=_current_input_tokens, added_chars=_added_chars,
                    )
                # 循环结束兜底：撞 MAX_PLANNER_TOOL_STEPS 上限仍无最终回答时，
                # 用主模型（thinking）基于现有上下文定稿一次补充最终回答（对齐 Codex）
                if not _final_text.strip():
                    _final_text = str(getattr(
                        _invoke_llm_with_retry(chat_openai, react_messages),
                        "content", "") or "").strip()
                # 空文本仅做一次通用兜底，绝不重试（对齐 Codex）
                if not _final_text.strip():
                    _final_text = "查询遇到问题，请稍后重试。"
            finally:
                reset_result_conversation(_conv_token)
                end_semantic_dedup(_sem_token)

            # 语义层命中审计由 execute_query 工具的 semantic.match 承担（工具内部按实际命中记录），
            # Planner 不再做分档路由/置信度判定，语义层定位交给工具在查数时确定

            # ── Planner 是唯一决策者：route 收敛为 respond 单一终态 ──
            # respond=给用户输出文本（澄清/确认/最终回答由 LLM 自定）；查数已在工具循环内通过 execute_query 完成
            route_llm = "respond"
            respond_text = _final_text

            def _respond_return(route_value, text, reason, extra=None):
                """respond 分支统一出口：写文本给用户并结束本轮（等用户回复）。"""
                _final_answer = text
                _ret = {
                    "route": route_value,
                    "respond_text": _final_answer,
                    "final_answer": _final_answer,
                    "effective_query": effective_query,
                    "planner_reason": reason,
                    "topic_status": "clarifying",  # respond 后必 END 等用户，对话继续
                    "messages": [
                        AIMessage(
                            content=_final_answer,
                            name="planner",
                            id=f"{state.get('request_id', '')}:respond",
                        )
                    ],
                    # 消费回环标记，避免残留影响后续轮次路由
                    "seeker_empty_result": False,
                    "seeker_plan_error": None,
                    "seeker_error_unresolvable": None,
                }
                if extra:
                    _ret.update(extra)
                log_node_end(
                    "planner",
                    route=route_value,
                    route_source="planner_llm",
                    reason=reason,
                    ms=elapsed_ms(timer),
                )
                log_state_snapshot("planner", {**state, **_ret})
                return _ret

            # respond 分支：直接输出文本给用户（澄清/确认/直接回答由 LLM 自定，空文本原样输出不再替模型兜底）
            planner_reason = "Planner 自由文本输出（澄清/回答）：" + (respond_text or "")[:120]
            return _respond_return("respond", respond_text, planner_reason)


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise
        finally:
            reset_execute_query_context(_exec_ctx)

    return planner_node
