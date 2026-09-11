# Planner 调度节点：元数据检索 → LLM 结构化解析 → 路由判定（A1 收敛两值）
#
# Planner 是唯一决策者：route ∈ {execute, respond}，execute=进执行链查数，respond=给用户输出文本。
# 流程：
#   ① 第0层拆检索词 + Planner ReAct 工具循环（search_semantic/search_tables/search_columns/probe_values/query_stored_result）
#   ② LLM 解析：输出 effective_query / route / respond_text / 槽位 / semantic_metrics（置信度）
#   ③ 路由：route=execute 时语义层确定性构建方案（未命中用最小方案直通）进执行链；
#       route=respond 时直接输出澄清/回答文本结束本轮
import re
from datetime import date
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage
from agentTest.langgraph_app.services.plan_synthesizer import (
    build_plan_from_semantic,
)
from agentTest.langgraph_app.services.query_plan_service import (
    _extract_time_from_filters,
    lock_query_plan,
)
from agentTest.langgraph_app.services.result_store import list_result_index
from agentTest.semantic_layer.metric_matcher import (
    grep_metrics_from_keywords,
)
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body
from agentTest.langgraph_app.prompts.planner_prompt import (
    PlannerOutput,
    SemanticKeywordsOutput,
    PLANNER_SYSTEM_PROMPT,
    PLANNER_KEYWORD_SYSTEM_PROMPT,
    PLANNER_KEYWORD_USER_TEMPLATE,
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
            content = content[:max_chars_per_msg] + "..."
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
        columns = ", ".join((entry.get("columns") or [])[:8])
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

def build_planner_node(runtime):
    # M2：Planner 工具化，从统一注册表取只读安全工具（元数据检索 + 落盘结果预览）
    planner_tools = runtime["tool_registry"].get(group="planner")

    # ChatOpenAI：LangChain 标准的 OpenAI 兼容客户端
    # 挂载 LLM 日志回调：记录 prompt/输出/耗时，便于 trace 回放
    chat_openai = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,                   # 判定任务不需要随机性
        extra_body=get_model_extra_body(),
        callbacks=[build_llm_logging_handler("planner")],
    )
    # with_structured_output：告诉 LLM 按 PlannerOutput 的格式返回 JSON
    structured_llm = chat_openai.with_structured_output(PlannerOutput)
    # M2：ReAct 工具循环 LLM，可自主调用 planner_tools 补充信息；最终仍由 structured_llm 输出 JSON
    react_llm = chat_openai.bind_tools(planner_tools)

    # 第0层关键词提取：独立小调用（只输出 semantic_keywords），避免拆词噪声进入完整解析
    keyword_structured_llm = chat_openai.with_structured_output(SemanticKeywordsOutput)
    keyword_prompt = ChatPromptTemplate.from_messages([
        ("system", PLANNER_KEYWORD_SYSTEM_PROMPT),
        ("human", PLANNER_KEYWORD_USER_TEMPLATE),
    ])

    def planner_node(state):
        # 每次调用只要求传入本轮输入
        current_user_input = state["current_user_input"]
        # 区分本轮触发来源：执行链回看（有结果待撰写回答）/ 0 行自愈回环 / 方案失败修复
        from_execution_review = state.get("execution_review") or False
        # Seeker 0 行自愈回环：上次执行成功但无数据，需判断是否过滤值不匹配
        from_empty_result = state.get("seeker_empty_result") or False

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

            # ── 第0层：LLM 拆业务检索词（对齐 skill 关键词 grep）──
            # 独立小调用只输出 semantic_keywords，避免拆词噪声进入完整解析
            history_context = _build_history_context(state.get("messages") or [])
            keyword_output = keyword_structured_llm.invoke(
                keyword_prompt.invoke({
                    "question": current_user_input,
                    "history": history_context or "无",
                })
            )
            semantic_keywords = [
                str(k).strip()
                for k in (keyword_output.semantic_keywords or [])
                if str(k).strip()
            ]
            log_sub_info(f"semantic_keywords: {semantic_keywords}", node_name="planner")

            # ── 第1层：语义层候选由 Planner ReAct 自主调用 search_semantic 获取 ──
            # 不再程序强制 grep/注入 prompt；命中指标 id 由 LLM 在 semantic_metrics 中声明，
            # 程序在步骤②后用 id 反查 provider 组装完整候选（见 _resolve_semantic_matches）
            semantic_matches = []
            # 候选表/字段：语义层命中后由反查结果填充；RAG 元数据由 agent 自主调 search_tables/search_columns
            table_candidates = []
            column_candidates = []

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
            seeker_plan_error = state.get("seeker_plan_error") or ""
            if seeker_plan_error:
                sections.append(
                    f"【上次执行失败原因（必须调整方案避开该问题）】\n{seeker_plan_error}"
                )
            if from_empty_result:
                _prev_plan = state.get("confirmed_plan") or {}
                _prev_sql = state.get("generated_sql") or ""
                _empty_rounds = state.get("empty_result_rounds") or 0
                _empty_hint = ""
                if _empty_rounds >= MAX_EMPTY_RESULT_ROUNDS:
                    _empty_hint = "\n已达重试上限，若确认无匹配数据请直接 route=respond 告知用户，不要继续探查重试。"
                sections.append(
                    "【上次执行 0 行反馈（SQL 执行成功但无数据，需先自行核实原因再决定动作）】\n"
                    f"方案过滤条件：{(_prev_plan.get('filters') or '无')}\n"
                    f"执行的 SQL：{str(_prev_sql)[:800] or '无'}\n"
                    "0 行可能有多种原因（过滤值与库中实际存储不一致、字段选错、数据本身为空等），"
                    "请先自行核实原因（可用 probe_values 探查实际取值、search_columns 核验字段）再决定："
                    "可修正后 route=execute 重跑，或确认确实无数据时 route=respond 直接告知用户。"
                    "这是查库可解决的事实问题，不要 route=respond 向用户询问。"
                    "请在 reason 中说明你发现 0 行的判断依据与本次修正动作（如改用模糊匹配确认实际值），便于向用户展示修正过程。"
                    f"{_empty_hint}"
                )
            if from_execution_review:
                _last_result = state.get("last_query_result") or {}
                _res_preview = _last_result.get("preview_rows") or []
                _res_columns = _last_result.get("columns") or []
                _res_id = _last_result.get("result_id") or ""
                _res_count = _last_result.get("row_count") or 0
                _res_csv = _last_result.get("full_csv") or ""
                _preview_lines = []
                if _res_preview:
                    _header = "| " + " | ".join(_res_columns) + " |"
                    _sep = "| " + " | ".join(["---"] * len(_res_columns)) + " |"
                    _preview_lines.append(_header)
                    _preview_lines.append(_sep)
                    for _row in _res_preview:
                        _preview_lines.append("| " + " | ".join(str(_row.get(c, "")) for c in _res_columns) + " |")
                sections.append(
                    "【上次执行结果（结果已落盘，请基于此撰写最终回答）】\n"
                    f"结果 ID：{_res_id} | 共 {_res_count} 行 | 列：{', '.join(_res_columns) or '无'}\n"
                    + ("\n".join(_preview_lines) if _preview_lines else "（无预览行）")
                    + (f"\n全量 CSV：{_res_csv}" if _res_csv else "")
                    + "\n\n【最终回答要求】\n"
                    + "前端支持 Markdown 渲染（含表格），请直接在 respond_text 中把上述预览行输出为 Markdown 表格，"
                    + "并在回答末尾明确给出全量 CSV 的完整保存路径（上面『全量 CSV』已是完整绝对路径）。"
                    + "禁止写『文本模式/界面无法展示表格』之类的说明，表格直接展示即可。"
                )
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
                for _round in range(MAX_PLANNER_RESPOND_RETRY + 1):
                    for _step in range(MAX_PLANNER_TOOL_STEPS):
                        _response = react_llm.invoke(react_messages)
                        react_messages.append(_response)
                        _tool_calls = getattr(_response, "tool_calls", None) or []
                        if not _tool_calls:
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
                    planner_output = structured_llm.invoke(react_messages)
                    _respond_empty = (
                        planner_output.route == "respond"
                        and not (planner_output.respond_text or "").strip()
                    )
                    if not _respond_empty:
                        break
                    # 回答未完成：追加通用提示，让 LLM 用工具补齐数据或给出实际内容（不枚举场景）
                    react_messages.append(SystemMessage(
                        content="你的 respond_text 为空，本轮不能结束。"
                                "若回答所需数据不在上下文中，请先调用工具获取；"
                                "然后在 respond_text 中写出给用户的实际内容（结果/澄清/确认）。"
                    ))
            finally:
                reset_result_conversation(_conv_token)

            effective_query = (
                    planner_output.effective_query.strip()
                    or current_user_input
            )
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

            # ── Planner 是唯一决策者：route ∈ {execute, respond} ──
            # execute=本轮要查数（进执行链）；respond=给用户输出文本（澄清/确认/最终回答由 LLM 自定）
            updated_plan = None
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

            # ── execute 分支：构建方案进执行链 ──
            execution_rounds = state.get("execution_rounds") or 0
            if execution_rounds >= MAX_EXECUTION_ROUNDS:
                # 查询轮次上限（防 execute 死循环，安全兜底）：本轮不再执行，respond 说明
                fallback_text = (
                    planner_output.reason.strip()
                    or "本轮已多次查询仍未完成，请补充信息或换个问法，我再为您查询。"
                )
                planner_reason = (
                    f"已达查询轮次上限({MAX_EXECUTION_ROUNDS})，本轮不再执行，respond："
                    + planner_output.reason
                )
                return _respond_return("respond", fallback_text, planner_reason, {
                    "evaluator_pending": False,
                })

            # 只采信 LLM 判定相关（>=候选阈值）的语义层命中
            _confirmed_ids = {
                str(m.id)
                for m in (planner_output.semantic_metrics or [])
                if float(m.confidence or 0) >= SEMANTIC_CONFIDENCE_CANDIDATE
            }
            metric_hits = [
                sc for sc in semantic_candidates
                if str(sc.get("id") or "") in _confirmed_ids
            ]
            _conf_by_id = {
                str(m.id): float(m.confidence or 0)
                for m in (planner_output.semantic_metrics or [])
            }
            metric_hits = sorted(
                metric_hits,
                key=lambda h: _conf_by_id.get(str(h.get("id") or ""), 0.0),
                reverse=True,
            )
            # 语义层确定性构建方案（语义层优先）；未命中时用 Planner 输出构造最小方案直通
            plan = build_plan_from_semantic(
                metric_hits=metric_hits,
                semantic_provider=runtime.get("semantic_metadata_provider"),
                dimension_mentions=planner_output.dimension_mentions,
                # 时间不再单独传槽位：统一由 filters 中的 yyyy-MM-dd 条件派生
                filters=planner_output.filters,
                # 每轮基于 effective_query 重建方案，不继承旧 confirmed_plan
                draft=None,
                complex_flag=planner_output.complex,
            )
            if plan is None:
                plan = _build_minimal_plan(planner_output)

            if plan is None:
                # 方案构建失败（信息不足）：不再降级 Advisor，respond 向用户澄清
                clarify_text = (
                    planner_output.reason.strip()
                    or "查询方案不完整，请补充指标、时间或过滤条件后我再为您查询。"
                )
                planner_reason = (
                    "Planner 判定 execute 但方案不完整，改为 respond 澄清："
                    + planner_output.reason
                )
                return _respond_return("respond", clarify_text, planner_reason)

            updated_plan = plan
            tables = plan.get("tables", [])
            fields = plan.get("fields", [])
            completeness = "full"
            route = "execute"
            # 0 行自愈轮在前缀体现修正语义，便于前端思考过程区分轮次
            _reason_prefix = (
                "Planner 判定 0 行自愈修正后可直接执行："
                if from_empty_result
                else "Planner 判定可直接执行，方案构建通过："
            )
            planner_reason = _reason_prefix + planner_output.reason

            # 语义层未命中且无候选时，用第0层检索词补一次 grep 写共享状态（供日志/trace 参考）
            if not semantic_candidates and semantic_keywords:
                semantic_candidates = grep_metrics_from_keywords(
                    semantic_keywords, limit=SEMANTIC_GREP_TOP_K
                )

            new_entities = {
                "effective_query": effective_query,
                "table": tables[0] if tables else "",
                "tables": tables,
                "fields": fields,
                "filters": planner_output.filters,
                # 时间范围从 filters 中的 yyyy-MM-dd 条件派生，不再依赖独立槽位
                "time_range": _extract_time_from_filters(planner_output.filters)[1],
                "complex": planner_output.complex,
                "completeness": completeness,
                "unresolved_dimensions": (updated_plan or {}).get("unresolved_dimensions") or [],
                "plan_error": seeker_plan_error,
                "table_candidates": table_candidates,
                "column_candidates": column_candidates,
                # 语义层 grep 候选（含 notes 命中）与 LLM 置信度，供日志/trace 参考
                "semantic_keywords": semantic_keywords,
                "semantic_metrics": [dict(m) for m in semantic_metrics],
                "semantic_candidates": semantic_candidates,
                "route": route,
            }

            log_node_end(
                "planner",
                route=route,
                route_source="planner_llm",
                completeness=completeness,
                tables=str(tables),
                fields=str(fields),
                high_sim_tables=high_similarity_table_count,
                high_sim_columns=high_similarity_column_count,
                unresolved_dimensions=(updated_plan or {}).get("unresolved_dimensions") or [],
                reason=planner_reason,
                ms=elapsed_ms(timer),
            )

            # Planner 路由结果决定 Topic 下一阶段：execute 进执行链，respond 等用户
            if route == "execute":
                next_topic_status = "confirmed"
            else:
                next_topic_status = "clarifying"

            # ── 去 pending 状态机：不再跨轮保存解析证据/候选快照 ──
            # 澄清候选由 Planner respond 写入历史消息，用户选择由 Planner 改写 effective_query 体现；
            # 本轮的 analysis_spec 只保留当轮意图，字段最终过元数据校验（validate_field_table_bindings 等）。
            existing_spec = state.get("analysis_spec") or {}

            metric_mentions = list(planner_output.metric_mentions or [])
            if not metric_mentions:
                metric_mentions = list(existing_spec.get("metric_mentions") or [])
            dimension_mentions = list(planner_output.dimension_mentions or [])
            if not dimension_mentions:
                dimension_mentions = list(existing_spec.get("dimension_mentions") or [])

            analysis_spec = dict(existing_spec)
            analysis_spec.update({
                "analysis_type": planner_output.analysis_type,
                "metric_mentions": metric_mentions,
                "dimension_mentions": dimension_mentions,
                # 语义层命中状态统一存于 planner_entities，analysis_spec 只保留业务意图
                "time_range": "",
                "time_grain": "",
                "filters": [],
                "order_by": [],
                "limit": 0,
                "comparison": {},
            })

            return_value = {
                "route": route,
                "respond_text": "",
                "planner_reason": planner_reason,
                "effective_query": effective_query,
                "planner_entities": new_entities,
                "topic_status": next_topic_status,
                "analysis_spec": analysis_spec,
                # 消费回环标记，避免残留影响后续轮次路由
                "seeker_empty_result": False,
                "execution_review": False,
                "seeker_plan_error": None,
                "seeker_error_unresolvable": None,
                # 本轮要执行，结果未落盘前不触发 Evaluator
                "evaluator_pending": False,
                # execute 轮次计数（capture 每轮用户输入重置，防死循环）
                "execution_rounds": execution_rounds + 1,
            }
            # 去 Topic 化：不再写入 original_question（当前需求以 effective_query 为准）
            # Planner 构建的 confirmed 方案直接交给执行链，无需用户确认环节
            return_value["confirmed_plan"] = updated_plan
            return_value["plans"] = [updated_plan]
            # 成功构建新方案即重置执行失败修复计数（不区分新问数/追问）
            return_value["plan_repair_rounds"] = 0
            # 执行失败修复：本轮已消费失败原因，清空并累计修复次数
            if seeker_plan_error:
                return_value["plan_repair_rounds"] = (state.get("plan_repair_rounds") or 0) + 1

            # 节点完成后记录 State 分层摘要，供 trace 查看数据流转
            log_state_snapshot("planner", {**state, **return_value})

            return return_value


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
