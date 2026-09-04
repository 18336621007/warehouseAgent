# Planner 调度节点：FAISS 检索增强元数据 → LLM 结构化解析 → 路由判定
#
# Planner 是唯一的路由者：由 LLM 判定本轮进入 Seeker（直接执行）还是 Advisor（澄清/核验）。
# 流程：
#   ① 语义层 grep + FAISS/BM25 检索增强元数据
#   ② LLM 解析：输出 effective_query / route / 槽位 / semantic_metrics（置信度）
#   ③ 路由：route=seeker 时由语义层确定性构建方案（plan_synthesizer），校验通过才执行；
#       构建失败或 route=advisor 时进入 Advisor
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage
from agentTest.langgraph_app.services.plan_synthesizer import (
    build_plan_from_semantic,
    finalize_draft_plan,
)
from agentTest.semantic_layer.metric_matcher import (
    format_metric_context,
    grep_metrics_from_keywords,
    resolve_metric_chain,
)
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body
from agentTest.langgraph_app.prompts.planner_prompt import (
    PlannerOutput,
    SemanticKeywordsOutput,
    PLANNER_SYSTEM_PROMPT,
    PLANNER_USER_TEMPLATE,
    PLANNER_KEYWORD_SYSTEM_PROMPT,
    PLANNER_KEYWORD_USER_TEMPLATE,
    METADATA_SECTION_TEMPLATE,
)
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_example_retrieved
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
from agentTest.langgraph_app.runtime.graph_logger import log_metric_event
from agentTest.langgraph_app.runtime.graph_logger import log_search_scores
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.langgraph_app.runtime.llm_log_handler import build_llm_logging_handler
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.config.planner import (
    TABLE_SEARCH_K,
    COLUMN_SEARCH_K,
    PER_TABLE_COLUMN_QUOTA,
    HIGH_SIMILARITY_THRESHOLD,
)
from agentTest.config.semantic import (
    SEMANTIC_UNIQUE_GAP_THRESHOLD,
    SEMANTIC_GREP_TOP_K,
    SEMANTIC_CONFIDENCE_UNIQUE,
    SEMANTIC_CONFIDENCE_CANDIDATE,
)
from agentTest.langgraph_app.message_utils import get_last_ai_content


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
            if not (msg_id.endswith(":advisor") or msg_id.endswith(":seeker")):
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

def _build_table_scope(table_docs_with_scores, top_k: int = TABLE_SEARCH_K) -> list[str]:
    """从表级召回结果提取表作用域（小写表名，按召回顺序），供字段级召回限定范围。"""
    scope = []
    for doc, _score in (table_docs_with_scores or [])[:top_k]:
        name = str(doc.metadata.get("table", "")).strip().lower()
        if name and name not in scope:
            scope.append(name)
    return scope


def _recall_columns(column_vector_store, question: str, table_scope: list[str]) -> list:
    """两段式字段召回：先在表作用域内逐表检索（每表按配额收敛），再全局检索兜底。

    返回 (doc, distance) 列表：表作用域内字段在前，全局兜底字段在后；
    兜底只补充不在表作用域内的字段，避免表级召回漏召导致真实字段丢失。
    """
    docs: list = []
    seen = set()

    def _key(doc) -> tuple:
        metadata = doc.metadata or {}
        field = metadata.get("column") or metadata.get("field") or ""
        return (str(metadata.get("table", "")), str(field))

    for table_name in table_scope:
        try:
            hits = column_vector_store.similarity_search_with_score(
                question,
                k=COLUMN_SEARCH_K,
                filter={"table": table_name},
                fetch_k=max(COLUMN_SEARCH_K * 5, 50),
            )
        except Exception:
            # 单表检索异常不阻断整体召回
            continue
        for doc, distance in hits[:PER_TABLE_COLUMN_QUOTA]:
            key = _key(doc)
            if key in seen:
                continue
            seen.add(key)
            docs.append((doc, distance))

    try:
        fallback = column_vector_store.similarity_search_with_score(
            question,
            k=COLUMN_SEARCH_K,
        )
    except Exception:
        fallback = []
    scope_set = set(table_scope)
    for doc, distance in fallback:
        table_name = str(doc.metadata.get("table", "")).strip().lower()
        if table_name in scope_set:
            continue
        key = _key(doc)
        if key in seen:
            continue
        seen.add(key)
        docs.append((doc, distance))
    return docs

def build_planner_node(runtime):
    # 三层索引中的表层和字段层
    table_vector_store = runtime["table_vector_store"]
    column_vector_store = runtime["column_vector_store"]
    bm25_retriever = runtime.get("bm25_retriever")

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

    # 第0层关键词提取：独立小调用（只输出 semantic_keywords），避免拆词噪声进入完整解析
    keyword_structured_llm = chat_openai.with_structured_output(SemanticKeywordsOutput)
    keyword_prompt = ChatPromptTemplate.from_messages([
        ("system", PLANNER_KEYWORD_SYSTEM_PROMPT),
        ("human", PLANNER_KEYWORD_USER_TEMPLATE),
    ])

    # 组装 Prompt：system 定义角色和规则，human 传入用户问题和检索到的元数据
    prompt = ChatPromptTemplate.from_messages([
        ("system", PLANNER_SYSTEM_PROMPT),
        ("human", PLANNER_USER_TEMPLATE),
    ])

    def planner_node(state):
        # 每次调用只要求传入本轮输入
        current_user_input = state["current_user_input"]

        # 去 Topic 化：不再使用 original_question 固定基线，
        # 当前需求由 LLM 结合【完整对话历史】+【本轮输入】每轮判断（query 改写 effective_query）


        # ── 去 Topic 化：不再向 prompt 注入【当前查询方案】（confirmed_context）──
        # Planner 完全依赖【完整对话历史】判断当前需求（历史含 Advisor/Seeker 最终回答的方案信息），
        # 避免上一轮遗留方案干扰新需求理解；confirmed_plan 仅作为状态供确认/执行链使用（不注入 prompt）
        confirmed_plan = state.get("confirmed_plan") or {}

        # Advisor 上轮回复既用于向量检索，也用于 LLM 理解简短选择
        advisor_last_answer = get_last_ai_content(
            state.get("messages") or [],
            "advisor",
        )

        if advisor_last_answer:
            advisor_last_answer = advisor_last_answer[:800]
        else:
            advisor_last_answer = ""

        # ── FAISS + LLM 评估流程 ──
        timer = start_timer()
        log_node_start("planner", question=current_user_input)

        try:
            # 将对话上下文组合成检索文本，使“1”“A”等简短回答具有候选语义
            retrieval_parts = [
                f"当前需求：{current_user_input}",
            ]

            if advisor_last_answer:
                retrieval_parts.append(
                    f"Advisor上轮候选或方案：{advisor_last_answer}"
                )

            if current_user_input.strip():
                retrieval_parts.append(
                    f"用户本轮回答：{current_user_input}"
                )

            retrieval_question = "\n".join(retrieval_parts)

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

            # ── 第1层：全文 grep 检索语义层指标（含 notes/definition，避免备注命中静默漏召）──
            semantic_matches = grep_metrics_from_keywords(
                semantic_keywords, limit=SEMANTIC_GREP_TOP_K
            )
            metric_context_text = format_metric_context(semantic_matches)
            log_metric_event(
                "semantic.grep",
                node_name="planner",
                mention=current_user_input[:100],
                keywords=semantic_keywords,
                hit_count=len(semantic_matches),
                metric_ids=[m.get("id", "") for m in semantic_matches],
                metric_names=[m.get("name", "") for m in semantic_matches],
                hit_types=[m.get("hit_type", "") for m in semantic_matches],
                grep_confidences=[
                    round(float(m.get("confidence", 0) or 0), 2)
                    for m in semantic_matches
                ],
            )

            # 强命中（名称/别名）视为语义层主导，短路跳过 FAISS 双路召回；
            # 弱命中/无命中保留 FAISS 补充物理字段（RAG 兜底）
            _grep_strong = any(
                str(m.get("hit_type", "")) == "strong"
                for m in semantic_matches
            )
            if _grep_strong:
                # 未走检索：置空后续日志/统计引用的检索结果，避免引用未定义变量
                table_docs_with_scores = []
                column_docs_with_scores = []
                metadata_lines = []
                for _sm in semantic_matches:
                    _src = _sm.get("source_model", "")
                    if not _src:
                        continue
                    # 第2层：指针导航（metric → semantic_model → physical），只打开小组信息，
                    # 把分区/物理字段带出来，供 LLM 生成 SQL 时参考（如无分区明细表需用日期字段过滤）
                    _chain = resolve_metric_chain(_sm.get("id", ""))
                    _phys = _chain.get("physical") or {}
                    _phys_note = ""
                    if _phys:
                        _part = "、".join(_phys.get("partition") or []) or "无分区"
                        _fields = list((_phys.get("fields") or {}).keys())
                        _phys_note = (
                            f"；分区字段：{_part}"
                            f"；物理字段：{', '.join(_fields[:30])}"
                        )
                    metadata_lines.append(
                        f"[表 {_src}, 相似度: 1.0000]\n语义层推荐口径：{_sm.get('name', '')}，"
                        f"来源表：{_src}，表达式：{_sm.get('expression', _sm.get('id', ''))}{_phys_note}"
                    )
                metadata_context = "\n".join(metadata_lines)
                table_candidates = [
                    {
                        "table": _sm.get("source_model", ""),
                        "score": 1.0,
                        "comment": f"语义层推荐：{_sm.get('name', '')}",
                    }
                    for _sm in semantic_matches
                    if _sm.get("source_model")
                ]
                column_candidates = []
            else:
                # ── 步骤①：FAISS 检索增强元数据（先召回表，再在召回表内召回字段）──
                # 表层检索：向量 + BM25 双路召回，合并去重后按向量分数排序
                table_docs_with_scores = table_vector_store.similarity_search_with_score(retrieval_question, k=TABLE_SEARCH_K)
                seen_tables = {str(doc.metadata.get("table", "")).strip().lower() for doc, _ in table_docs_with_scores}
                if bm25_retriever:
                    try:
                        bm25_results = bm25_retriever.retrieve(retrieval_question, top_k=TABLE_SEARCH_K * 3)
                        for doc, bm25_score in bm25_results:
                            table_name = str(doc.metadata.get("table", "")).strip().lower()
                            if table_name and table_name not in seen_tables:
                                seen_tables.add(table_name)
                                max_vec_score = table_docs_with_scores[0][1] if table_docs_with_scores else 1.0
                                normalized_score = bm25_score / (bm25_score + 1.0) * max_vec_score
                                table_docs_with_scores.append((doc, normalized_score))
                    except Exception:
                        pass
                table_scope = _build_table_scope(table_docs_with_scores)
                column_docs_with_scores = _recall_columns(column_vector_store, retrieval_question, table_scope)

                # 拼接元数据上下文：表层在前，字段层在后
                metadata_lines = []
                for doc, score in table_docs_with_scores:
                    content = doc.page_content[:500]
                    table_name = doc.metadata.get("table", "")
                    metadata_lines.append(f"[表 {table_name}, 相似度: {score:.4f}]\n{content}")
                for doc, score in column_docs_with_scores:
                    content = doc.page_content[:300]
                    metadata_lines.append(f"[字段]\n{content}")
                metadata_context = "\n\n".join(metadata_lines)

                # 构建候选池（带分数+注释），供 Advisor 精排使用
                table_candidates = []
                for doc, score in table_docs_with_scores:
                    table_candidates.append({
                        "table": doc.metadata.get("table", ""),
                        "score": float(round(float(score), 4)),
                        "comment": (doc.page_content or "")[:200]
                    })
                column_candidates = []
                for doc, score in column_docs_with_scores:
                    column_candidates.append({
                        "table": doc.metadata.get("table", ""),
                        "field": doc.metadata.get("field", doc.metadata.get("column", "")),
                        "score": float(round(float(score), 4)),
                        "comment": (doc.page_content or "")[:200]
                    })

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


            # ── 步骤②：LLM 结构化解析（含 current_user_input、advisor_last_answer）──
            # 组装用户消息 sections：有内容的才带标题，避免空标题占用 token
            # （history_context 已在第0层关键词提取时计算）
            metadata_section = (
                METADATA_SECTION_TEMPLATE.format(metadata_context=metadata_context)
                if metadata_context.strip() and not _grep_strong
                else ""
            )
            sections = [f"【当前需求基线】\n{current_user_input}"]
            seeker_plan_error = state.get("seeker_plan_error") or ""
            if seeker_plan_error:
                sections.append(
                    f"【上次执行失败原因（必须调整方案避开该问题）】\n{seeker_plan_error}"
                )
            if metric_context_text:
                sections.append(f"【语义层指标候选】\n{metric_context_text}")
            if history_context:
                sections.append(f"【对话历史（最近 N 轮）】\n{history_context}")
            if metadata_section:
                sections.append(metadata_section)
            if example_context:
                sections.append(f"【历史相似问题】\n{example_context}")
            user_content = "\n\n".join(sections)
            prompt_value = prompt.invoke({"sections": user_content})
            planner_output = structured_llm.invoke(prompt_value)

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
            elif (
                len(semantic_matches) == 1
                and str(semantic_matches[0].get("hit_type", "")) == "strong"
            ):
                # LLM 未输出语义判定但 grep 唯一强命中：回退按语义层唯一短路
                semantic_metrics = [{
                    "id": semantic_matches[0]["id"],
                    "confidence": SEMANTIC_CONFIDENCE_UNIQUE,
                    "mention": "",
                }]
                _top_confidence = SEMANTIC_CONFIDENCE_UNIQUE
                _semantic_unique = True
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

            # 使用 LLM 还原后的完整需求重新探测候选数量，避免“1”“A”等短回答携带整组选项
            # 语义层唯一命中时跳过，指标口径已由语义层确定
            if not _semantic_unique:
                ambiguity_table_docs_with_scores = (
                    table_vector_store.similarity_search_with_score(
                        effective_query,
                        k=TABLE_SEARCH_K,
                    )
                )
                ambiguity_table_scope = _build_table_scope(ambiguity_table_docs_with_scores)
                ambiguity_column_docs_with_scores = _recall_columns(
                    column_vector_store,
                    effective_query,
                    ambiguity_table_scope,
                )
            else:
                ambiguity_table_docs_with_scores = []
                ambiguity_column_docs_with_scores = []
                ambiguity_table_scope = set()
                ambiguity_column_docs_with_scores = []



            # 兜底：LLM 未填 completeness 或填了无效值
            if completeness not in ("full", "partial", "none"):
                if not tables:
                    completeness = "none"
                elif not fields:
                    completeness = "partial"
                else:
                    completeness = "full"


            # ── 收集 top-k 分数，用于辅助日志 ──
            table_scores = [
                {
                    "name": doc.metadata.get("table", "?"),
                    "score": round(float(score), 3),
                }
                for doc, score in table_docs_with_scores
            ]

            column_scores = [
                {
                    "name": doc.metadata.get("column", "?"),
                    "score": round(float(score), 3),
                }
                for doc, score in column_docs_with_scores
            ]

            # ── 步骤③：基于完整有效需求统计各层高相似度候选数量 ──
            high_similarity_table_count = 0
            for doc, score in ambiguity_table_docs_with_scores:
                similarity = float(score)
                if similarity > HIGH_SIMILARITY_THRESHOLD:
                    high_similarity_table_count += 1

            high_similarity_column_count = 0
            selected_tables = set(tables)

            for doc, score in ambiguity_column_docs_with_scores:
                # 字段歧义只在 Planner 已确定的目标表内统计
                document_table = doc.metadata.get("table", "")
                if selected_tables and document_table not in selected_tables:
                    continue

                similarity = float(score)
                if similarity > HIGH_SIMILARITY_THRESHOLD:
                    high_similarity_column_count += 1

            # ── Planner 是唯一路由者：LLM 判定 route，程序只做方案安全网 ──
            updated_plan = None
            route_llm = planner_output.route or "advisor"
            draft_plan = (
                confirmed_plan
                if (confirmed_plan or {}).get("status") == "draft"
                else None
            )

            if route_llm == "seeker":
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
                if not metric_hits and semantic_candidates:
                    # LLM 未输出语义判定但 grep 唯一强命中时，回退采信最强候选
                    metric_hits = [semantic_candidates[0]]
                _conf_by_id = {
                    str(m.id): float(m.confidence or 0)
                    for m in (planner_output.semantic_metrics or [])
                }
                metric_hits = sorted(
                    metric_hits,
                    key=lambda h: _conf_by_id.get(str(h.get("id") or ""), 0.0),
                    reverse=True,
                )
                # 语义层确定性构建方案；失败时回退 Advisor 已落盘的完整草稿
                plan = build_plan_from_semantic(
                    metric_hits=metric_hits,
                    semantic_provider=runtime.get("semantic_metadata_provider"),
                    dimension_mentions=planner_output.dimension_mentions,
                    time_range=planner_output.time_range,
                    filters=planner_output.filters,
                    draft=draft_plan,
                    complex_flag=planner_output.complex,
                )
                if plan is None and draft_plan:
                    plan = finalize_draft_plan(draft_plan)
                if plan is not None:
                    updated_plan = plan
                    tables = plan.get("tables", [])
                    fields = plan.get("fields", [])
                    completeness = "full"
                    route = "seeker"
                    planner_reason = (
                        "Planner 判定可直接执行，语义层确定性构建方案通过："
                        + planner_output.reason
                    )
                else:
                    route = "advisor"
                    planner_reason = (
                        "Planner 判定 seeker 但方案构建失败，降级 Advisor 澄清："
                        + planner_output.reason
                    )
            else:
                route = "advisor"
                planner_reason = (
                    "Planner 判定需要先澄清/核验，进入 Advisor："
                    + planner_output.reason
                )

            new_entities = {
                # Advisor 后续使用完整有效需求，不能直接拿“1”“A”检索
                "effective_query": effective_query,
                "table": tables[0] if tables else "",
                "tables": tables,
                "fields": fields,
                "measures": [],
                "dimensions": [],
                "time_field": "pt_dt",
                "filters": planner_output.filters,
                "time_range": planner_output.time_range,
                "complex": planner_output.complex,
                "completeness": completeness,
                "plan_error": seeker_plan_error,
                "table_candidates": table_candidates,
                "column_candidates": column_candidates,
                "follow_up_mode": planner_output.follow_up_mode,
                # 语义层 grep 候选（含 notes 命中）与 LLM 置信度，供 Advisor 复用，
                # 避免 Advisor 词法匹配漏掉备注命中（如"调出"）
                "semantic_keywords": semantic_keywords,
                "semantic_metrics": [dict(m) for m in semantic_metrics],
                "semantic_candidates": semantic_candidates,
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
                reason=planner_reason,
                ms=elapsed_ms(timer),
            )
            log_search_scores("planner", "table", table_scores)
            log_search_scores("planner", "column", column_scores)

            # Planner 路由结果决定 Topic 下一阶段
            if route == "seeker":
                next_topic_status = "confirmed"
            else:
                next_topic_status = "clarifying"


            # ── 去 pending 状态机：不再跨轮保存解析证据/候选快照 ──
            # 澄清候选由 Advisor 写入历史消息，用户选择由 Planner 改写 effective_query 体现；
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
                "planner_reason": planner_reason,
                "effective_query": effective_query,
                "planner_entities": new_entities,
                "topic_status": next_topic_status,
                "analysis_spec": analysis_spec,
                "follow_up_mode": planner_output.follow_up_mode,  # 供 web 层决定后续处理
            }
            # 去 Topic 化：不再写入 original_question（当前需求以 effective_query 为准）
            # 用户最终确认后，写回 status=confirmed 的查询方案
            if updated_plan is not None:
                return_value["confirmed_plan"] = updated_plan
            elif planner_output.follow_up_mode == "new_query":
                # 新问数：清空历史遗留方案，避免旧方案干扰新需求理解
                return_value["confirmed_plan"] = None
                # 新问数重置执行失败修复计数，给每个新问题一次修复机会
                return_value["plan_repair_rounds"] = 0
            # 执行失败修复：本轮已消费失败原因，清空并累计修复次数
            if seeker_plan_error:
                return_value["seeker_plan_error"] = None
                return_value["plan_repair_rounds"] = (state.get("plan_repair_rounds") or 0) + 1

            log_sub_info(f"follow_up_mode: {planner_output.follow_up_mode}", node_name="planner")

                        # 节点完成后记录 State 分层摘要，供 trace 查看数据流转
            log_state_snapshot("planner", {**state, **return_value})

            return return_value


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
