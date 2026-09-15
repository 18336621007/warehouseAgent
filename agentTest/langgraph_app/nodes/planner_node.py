# Planner 调度节点（单 Agent / Codex 模式）：元数据检索 → LLM 解析 → ReAct 循环内查数写回答
#
# Planner 是唯一 Agent：查数通过 execute_query 工具在循环内完成，route 收敛为 respond 单一终态（直接输出文本）。
# 流程：
#   ① 第0层拆检索词 + Planner ReAct 工具循环（search_semantic/search_tables/search_columns/probe_values/query_stored_result/execute_query）
#   ② LLM 解析：输出 effective_query / route / respond_text / 槽位 / semantic_metrics（置信度）
#   ③ 查数通过 execute_query 工具在循环内完成（结果回填后直接写回答）；route 一律 respond，直接输出澄清/回答文本结束本轮
import json
import re
from datetime import date
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
    get_llm_fast_extra_body,
)
from agentTest.langgraph_app.prompts.planner_prompt import (
    PlannerOutput,
    PLANNER_SYSTEM_PROMPT,
)
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_example_retrieved
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
from agentTest.langgraph_app.runtime.graph_logger import log_metric_event
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
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.config.planner import (
    MAX_PLANNER_TOOL_STEPS,
    MAX_PLANNER_RESPOND_RETRY,
    MAX_EMPTY_RESULT_ROUNDS,
    MAX_EXECUTION_ROUNDS,
)
from agentTest.config.semantic import (
    SEMANTIC_UNIQUE_GAP_THRESHOLD,
    SEMANTIC_GREP_TOP_K,
    SEMANTIC_CONFIDENCE_UNIQUE,
    SEMANTIC_CONFIDENCE_CANDIDATE,
)


def _build_history_context(messages, max_turns=10, max_chars_per_msg=500):
    """把最近几轮用户消息与最终回答组装成对话历史，过滤工具消息与 ReAct 中间步骤。"""
    from langchain_core.messages import ToolMessage, AIMessage
    lines = []
    for msg in (messages or [])[-max_turns * 2:]:
        name = getattr(msg, "name", "") or ""
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
            msg_id = str(getattr(msg, "id", "") or "")
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


def _build_recent_candidates_text(recent_shown_candidates, resolutions=None):
    """把最近展示候选组装成精简事实文本，供 Planner 判断用户选择。

    候选不带程序编号：编号由模型在澄清文案中定义，模型需结合对话历史中的
    展示文案还原“编号→字段”映射；已确认概念回退展示候选快照，供改选指代参照。
    """
    lines = []
    for group in recent_shown_candidates or []:
        mention = group.get("mention", "")
        candidates = group.get("candidates") or []
        if not mention or not candidates:
            continue
        lines.append(f"[{mention} 最近展示候选]")
        for candidate in candidates:
            field = candidate.get("field", "")
            table = str(candidate.get("table") or "").split(".")[-1]
            comment = str(candidate.get("comment") or "").strip()
            lines.append(f"- {field}（含义：{comment}，表：{table}）")
    if not lines:
        for resolution in (resolutions or []):
            if resolution.get("status") != "resolved":
                continue
            candidates = resolution.get("candidates") or []
            if len(candidates) <= 1:
                continue
            lines.append(f"[{resolution.get('mention', '')} 历史展示候选（改选时参考）]")
            for candidate in candidates:
                field = candidate.get("field", "")
                table = str(candidate.get("table") or "").split(".")[-1]
                comment = str(candidate.get("comment") or "").strip()
                lines.append(f"- {field}（含义：{comment}，表：{table}）")
    return "\n".join(lines)

def _resolve_semantic_matches(semantic_metrics, provider) -> list[dict]:
    """用 Planner LLM 声明的指标 id 反查语义层完整口径（来源表/表达式/维度/备注）。

    search_semantic 工具只负责把候选展示给 LLM，命中判定与置信度由 LLM 输出；
    程序据此反查 provider 拿权威定义，供 build_plan_from_semantic 确定性构建与 Advisor 复用。
    """
    if not semantic_metrics or provider is None:
        return []
    matches = []
    for m in semantic_metrics:
        metric = provider.get_metric_by_id(str(m.id or ""))
        if not metric:
            continue
        entry = dict(metric)
        entry["confidence"] = float(m.confidence or 0)
        entry["mention"] = str(m.mention or "")
        matches.append(entry)
    return matches


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

def _try_parse_planner_output(response):
    """尝试把 ReAct 轮输出文本直接解析为 PlannerOutput；失败返回 None（成功则省一次定稿 LLM 调用）。"""
    if response is None:
        return None
    text = getattr(response, "content", None)
    text = str(text or "").strip()
    if not text:
        return None
    # 兼容 ```json ... ``` 代码块包裹
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return PlannerOutput.model_validate(data)
    except Exception:
        return None


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
        # 最终回答流式：structured 定稿生成 JSON 时实时提取 respond_text 推前端
        answer_field="respond_text",
    )
    # with_structured_output：结构化方式由配置 LLM_STRUCTURED_OUTPUT_METHOD 驱动
    # （qwen thinking 模式用 json_mode/response_format，自定义 ChatModel 统一处理）
    structured_llm = chat_openai.with_structured_output(PlannerOutput)
    # M2：ReAct 工具循环 LLM，可自主调用 planner_tools 补充信息；最终仍由 structured_llm 输出 JSON
    react_llm = chat_openai.bind_tools(planner_tools)

    # 方案2：最终回答用快速模型（关闭 thinking）格式化输出省时；模型名可用 LLM_FAST_MODEL 覆盖
    chat_openai_fast = ThinkingStreamChatModel(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_llm_fast_model(),
        extra_body=get_llm_fast_extra_body(),
        callbacks=[build_llm_logging_handler("planner_fast")],
        # 最终回答流式：结构化定稿生成 JSON 时实时提取 respond_text 推前端
        answer_field="respond_text",
    )
    structured_llm_fast = chat_openai_fast.with_structured_output(PlannerOutput)

    def planner_node(state):
        # 每次调用只要求传入本轮输入
        current_user_input = state["current_user_input"]
        # 单 Agent 模式：无执行链回环（查数由 execute_query 工具在循环内完成）
        from_execution_review = False
        from_empty_result = False
        # 注入查数工具上下文（request/会话归属，工具内部日志与落盘用）
        _exec_ctx = set_execute_query_context(
            state.get("request_id", ""),
            state.get("conversation_id", ""),
            state.get("topic_id", ""),
        )

        # 去 Topic 化：不再使用 original_question 固定基线，
        # 当前需求由 LLM 结合【完整对话历史】+【本轮输入】每轮判断（query 改写 effective_query）


        # ── 去 Topic 化：不再向 prompt 注入【当前查询方案】（confirmed_context）──
        # Planner 完全依赖【完整对话历史】判断当前需求（历史含 Advisor/Seeker 最终回答的方案信息），
        # 避免上一轮遗留方案干扰新需求理解；confirmed_plan 仅作为状态供确认/执行链使用（不注入 prompt）
        confirmed_plan = state.get("confirmed_plan") or {}

        # ── LLM 评估流程 ──
        timer = start_timer()
        log_node_start("planner", question=current_user_input)

        try:

            # ── 第0层：组装对话历史（方案1后不再独立调用关键词提取，semantic_keywords 取最终输出）──
            history_context = _build_history_context(state.get("messages") or [])

            # ── 第1层：语义层候选由 Planner ReAct 自主调用 search_semantic 获取 ──
            # 不再程序强制 grep/注入 prompt；命中指标 id 由 LLM 在 semantic_metrics 中声明，
            # 程序在步骤②后用 id 反查 provider 组装完整候选（见 _resolve_semantic_matches）
            semantic_matches = []
            # 候选表：语义层命中后由反查结果填充；RAG 元数据由 agent 自主调 search_tables/search_columns
            table_candidates = []

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


            # ── 步骤②：LLM 结构化解析 ──
            # 组装用户消息 sections：有内容的才带标题，避免空标题占用 token
            # （history_context 已在第0层关键词提取时计算；M2b 后元数据由工具自主检索，不再程序注入）
            sections = [
                f"【当前日期】\n{date.today().isoformat()}",
                f"【当前需求基线】\n{current_user_input}",
            ]
            if history_context:
                sections.append(f"【对话历史（最近 N 轮）】\n{history_context}")
            if example_context:
                sections.append(f"【历史相似问题】\n{example_context}")
            # 注入最近几轮查询结果索引（只含摘要），供模型识别"第三轮/刚才的结果"等指代
            result_index = list_result_index(state.get("conversation_id") or "", limit=8)
            if result_index:
                sections.append("【最近查询结果索引】\n" + _format_result_index(result_index))
            user_content = "\n\n".join(sections)

            # ── M2：Planner ReAct 工具循环（自主决定是否补充检索/读落盘结果）──
            # skill 指令注入：命中 skill 时在系统提示后追加，作为背景决策策略（不暴露给前端思考过程）
            skill_manager = runtime.get("skill_manager")
            matched_skills = (
                skill_manager.match_skills(current_user_input, scope="planner")
                if skill_manager else []
            )
            if matched_skills:
                log_skill_event(
                    "planner",
                    name=",".join(s.name for s in matched_skills),
                    hit_count=len(matched_skills),
                )
            skill_text = (
                skill_manager.format_instruction(current_user_input, scope="planner")
                if skill_manager else ""
            )
            react_messages = [
                SystemMessage(content=PLANNER_SYSTEM_PROMPT),
            ]
            if skill_text:
                react_messages.append(SystemMessage(content=skill_text))
            react_messages.append(HumanMessage(content=user_content))
            planner_tool_map = {t.name: t for t in planner_tools}
            # query_stored_result 依赖会话上下文：循环期间注入，结束后复位
            _conv_token = set_result_conversation(str(state.get("conversation_id") or ""))
            try:
                # respond 但无实际内容视为"回答未完成"，给 LLM 补工具/补内容后再定稿（通用完整性约束）
                planner_output = None
                # 方案2：模型主动停止工具调用（信息已充分）时用快速模型定稿，省 thinking 时间；
                # 快速模型定稿失败（execute/空回答）后回退 thinking，保证判断质量
                _fast_failed = False
                for _round in range(MAX_PLANNER_RESPOND_RETRY + 1):
                    _tool_break = False
                    _use_fast = False
                    _direct_output = None
                    for _step in range(MAX_PLANNER_TOOL_STEPS):
                        _response = react_llm.invoke(react_messages)
                        react_messages.append(_response)
                        _tool_calls = getattr(_response, "tool_calls", None) or []
                        if not _tool_calls:
                            _tool_break = True
                            # 尝试直接解析该轮输出为 PlannerOutput（成功则省一次定稿 LLM 调用）
                            _direct_output = _try_parse_planner_output(_response)
                            break
                        log_tools_called("planner", [str(tc.get("name", "?")) for tc in _tool_calls])
                        for _tc in _tool_calls:
                            _tool = planner_tool_map.get(_tc.get("name"))
                            if _tool is None:
                                _result = f"未知工具: {_tc.get('name')}"
                            else:
                                try:
                                    _result = _tool.invoke(_tc.get("args") or {})
                                except Exception as _err:
                                    _result = f"工具调用失败: {_err}"
                            react_messages.append(ToolMessage(
                                content=str(_result)[:2000],
                                tool_call_id=_tc.get("id"),
                            ))
                    if _direct_output is not None:
                        # react 轮已直接输出结构化 JSON：直接使用，跳过定稿 LLM 调用
                        planner_output = _direct_output
                    else:
                        _use_fast = _tool_break and not _fast_failed
                        planner_output = (
                            structured_llm_fast if _use_fast else structured_llm
                        ).invoke(react_messages)
                    # 单 Agent：查数在工具循环内通过 execute_query 完成，route 收敛为 respond 单一终态；
                    # 仅 respond 且文本为空（回答未完成）时重试补齐，不再拦截 route=execute
                    _needs_retry = (
                        planner_output.route == "respond"
                        and not (planner_output.respond_text or "").strip()
                    )
                    if not _needs_retry:
                        break
                    # 回答未完成：追加通用提示，让 LLM 用工具补齐数据或给出实际内容（不枚举场景）
                    react_messages.append(SystemMessage(
                        content="你的 respond_text 为空，本轮不能结束。"
                                "若回答所需数据不在上下文中，请先调用工具获取；"
                                "然后在 respond_text 中写出给用户的实际内容（结果/澄清/确认）。"
                    ))
                    # fast 定稿失败（execute/空回答）：后续轮回退 thinking，避免反复快速失败
                    if _use_fast:
                        _fast_failed = True
            finally:
                reset_result_conversation(_conv_token)

            effective_query = (
                    planner_output.effective_query.strip()
                    or current_user_input
            )
            # 方案1：semantic_keywords 直接取最终结构化输出（不再独立调用，省一次 LLM 调用）
            semantic_keywords = [
                str(k).strip()
                for k in (planner_output.semantic_keywords or [])
                if str(k).strip()
            ]
            log_sub_info(f"semantic_keywords: {semantic_keywords}", node_name="planner")
            # ── 信任 LLM 的 completeness 判定，不做覆盖 ──
            tables = planner_output.tables
            fields = planner_output.fields
            completeness = planner_output.completeness

            # ── 第3层：LLM 置信度 → 分档路由（对齐 skill 置信度规则）──
            # >=0.9 唯一强命中短路；0.55~0.9 候选反问；<0.55 走 RAG
            semantic_metrics = sorted(
                (m for m in (planner_output.semantic_metrics or [])),
                key=lambda m: float(m.confidence or 0),
                reverse=True,
            )
            # 用 LLM 声明的指标 id 反查语义层完整口径（权威定义，供确定性构建与 Advisor 复用）
            semantic_matches = _resolve_semantic_matches(
                semantic_metrics,
                runtime.get("semantic_metadata_provider"),
            )
            if semantic_matches:
                # 语义层命中：候选表来自语义层推荐，供 Advisor/Seeker 参考
                table_candidates = [
                    {
                        "table": _sm.get("source_model", ""),
                        "score": 1.0,
                        "comment": f"语义层推荐：{_sm.get('name', '')}",
                    }
                    for _sm in semantic_matches
                    if _sm.get("source_model")
                ]
            if semantic_metrics:
                _top_confidence = max(
                    (float(m.confidence or 0) for m in semantic_metrics),
                    default=0.0,
                )
                _semantic_unique = (
                    len(semantic_metrics) == 1
                    and _top_confidence >= SEMANTIC_CONFIDENCE_UNIQUE
                ) or (
                    len(semantic_metrics) >= 2
                    and _top_confidence >= SEMANTIC_CONFIDENCE_UNIQUE
                    and (
                        float(semantic_metrics[0].confidence or 0)
                        - float(semantic_metrics[1].confidence or 0)
                    ) >= SEMANTIC_UNIQUE_GAP_THRESHOLD
                )
            else:
                semantic_metrics = []
                _top_confidence = 0.0
                _semantic_unique = False

            # 分档：unique=唯一强命中短路；candidate=候选反问；rag=走检索召回
            if _semantic_unique:
                _tier = "unique"
            elif _top_confidence >= SEMANTIC_CONFIDENCE_CANDIDATE:
                _tier = "candidate"
            else:
                _tier = "rag"

            # 语义层命中日志：记录每个指标的分数/置信度与短路判定，
            # 便于排查"走了语义层还是召回"
            log_metric_event(
                "semantic.match",
                node_name="planner",
                mention=current_user_input[:100],
                hit_count=len(semantic_matches),
                metric_ids=[m.get("id", "") for m in semantic_matches],
                metric_names=[m.get("name", "") for m in semantic_matches],
                metric_scores=[m.get("score", 0) for m in semantic_matches],
                metric_confidences=[
                    round(float(m.get("confidence", 0) or 0), 2)
                    for m in semantic_matches
                ],
                top_confidence=round(_top_confidence, 2),
                semantic_unique=_semantic_unique,
                tier=_tier,
            )

            # 语义层候选：完整指标信息 + LLM 置信度，供 Advisor 复用（避免 Advisor 词法漏召）
            semantic_candidates = [dict(m) for m in semantic_matches]
            _conf_by_id = {
                str(m.id): float(m.confidence or 0)
                for m in (planner_output.semantic_metrics or [])
            }
            for _sc in semantic_candidates:
                _sc["confidence"] = _conf_by_id.get(
                    _sc.get("id", ""), _sc.get("confidence", 0.0)
                )
                # score 统一为 grep 得分，供 Advisor/日志展示使用
                _sc.setdefault("score", _sc.get("grep_score", 0))

            # M2b：移除 effective_query 二次 FAISS 探测，高相似度统计随之置 0
            # 兜底：LLM 未填 completeness 或填了无效值
            if completeness not in ("full", "partial", "none"):
                if not tables:
                    completeness = "none"
                elif not fields:
                    completeness = "partial"
                else:
                    completeness = "full"


            # M2b：FAISS 检索移除后不再有分数与高相似度统计，置 0 保持日志结构稳定
            high_similarity_table_count = 0
            high_similarity_column_count = 0

            # ── Planner 是唯一决策者：route 收敛为 respond 单一终态 ──
            # respond=给用户输出文本（澄清/确认/最终回答由 LLM 自定）；查数已在工具循环内通过 execute_query 完成
            route_llm = planner_output.route or "respond"
            respond_text = (planner_output.respond_text or "").strip()

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
                    "execution_review": False,
                    "seeker_plan_error": None,
                    "seeker_error_unresolvable": None,
                    # 仅执行回看触发的 respond（基于结果写回答）才触发 Evaluator
                    "evaluator_pending": bool(from_execution_review),
                    # 语义层命中/候选快照（供日志/trace 参考，respond 不消费）
                    "planner_entities": {
                        "effective_query": effective_query,
                        "tables": tables,
                        "fields": fields,
                        "completeness": completeness,
                        "semantic_metrics": [dict(m) for m in semantic_candidates],
                        "semantic_candidates": semantic_candidates,
                        "table_candidates": table_candidates,
                        "route": route_value,
                    },
                }
                if extra:
                    _ret.update(extra)
                log_node_end(
                    "planner",
                    route=route_value,
                    route_source="planner_llm",
                    completeness=completeness,
                    tables=str(tables),
                    fields=str(fields),
                    high_sim_tables=high_similarity_table_count,
                    high_sim_columns=high_similarity_column_count,
                    reason=reason,
                    ms=elapsed_ms(timer),
                )
                log_state_snapshot("planner", {**state, **_ret})
                return _ret

            if route_llm == "respond":
                # respond 分支：直接输出文本给用户（澄清/确认/直接回答由 LLM 自定）
                if not respond_text:
                    # 回答未完成且已达重试上限：用通用引导兜底，不暴露内部 reason
                    respond_text = "请补充最关键的指标、维度或过滤条件，我好继续为您查询。"

                planner_reason = "Planner 判定 respond（澄清/回答）：" + planner_output.reason
                return _respond_return("respond", respond_text, planner_reason)

            # ── route 非 respond 兜底（防御）：单 Agent 下 route 已收敛为 respond，
            # LLM 异常输出 execute 等值时按 respond 处理，避免崩溃 ──
            fallback_text = (
                planner_output.reason.strip()
                or "请补充查询条件，我再为您查询。"
            )
            planner_reason = (
                "Planner 输出异常 route（非 respond），兜底改为 respond："
                + planner_output.reason
            )
            return _respond_return("respond", fallback_text, planner_reason)


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise
        finally:
            reset_execute_query_context(_exec_ctx)

    return planner_node
