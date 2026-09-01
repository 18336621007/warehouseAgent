# Planner 调度节点：FAISS 检索增强元数据 → LLM 结构化解析 → 模糊度判定
#
# Planner 是唯一的调度中心：LLM 语义判断为主，FAISS 仅做 full 时的极端兜底。
# 三步流程（对齐论文 SQL-MARS 的 Planner 设计）：
#   ① FAISS 检索：用余弦相似度召回 top-k 增强元数据
#   ② LLM 解析：将召回元数据 + 用户问题 + 用户实际输入 + 已确认方案 + Advisor 上轮回复传给 LLM
#   ③ 阈值判定：模糊需求进入 Advisor；明确需求先由 Advisor 锁定方案；
#       只有用户最终确认 locked 方案后才能进入 Seeker
from copy import deepcopy

from langchain.agents.middleware.todo import Todo
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage
from agentTest.langgraph_app.services.query_plan_service import confirm_query_plan
from agentTest.langgraph_app.services.metric_clarification_service import MetricClarificationService
from agentTest.semantic_layer.metric_matcher import (
    format_metric_context,
    match_metrics_from_query,
)
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE, METADATA_SECTION_TEMPLATE
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, log_node_event
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
    MAX_HIGH_SIMILARITY_COUNT
)
from agentTest.config.semantic import SEMANTIC_UNIQUE_GAP_THRESHOLD
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


def _apply_user_selection_to_draft(confirmed_plan: dict, selected_resolution: dict) -> dict:
    """用户选择/改选口径后，Planner 将选择落到共享方案草稿：
    更新该概念的 concept_resolutions，并把 measures/fields/order_by 中旧字段替换为最新选择，
    方案状态回到 draft（Advisor 下一轮在此基础上继续完善或直接锁定）。

    仅当方案已存在且能反查该概念旧字段时才改写；否则返回 None，
    由 Advisor 通过 update_draft_plan 落草稿，避免凭空造方案。
    """
    plan = deepcopy(confirmed_plan or {})
    if not plan or not selected_resolution:
        return None
    mention = str(selected_resolution.get("mention") or "")
    new_field = str(selected_resolution.get("selected_field") or "")
    concept_type = str(selected_resolution.get("concept_type") or "metric")
    # 指标概念落 measures，维度概念落 dimensions
    target_key = "dimensions" if concept_type == "dimension" else "measures"
    if not mention or not new_field:
        return None
    concepts = plan.get("concept_resolutions") or {}
    if not isinstance(concepts, dict):
        concepts = {}
    old_field = ""
    previous = concepts.get(mention)
    if isinstance(previous, dict):
        old_field = str(previous.get("field") or "")
    if not old_field:
        # 方案尚未记录该概念解析：确认的是新维度字段时直接落到 dimensions，
        # 保证“负责人”这类属性字段不丢失；指标概念仍交由 Advisor 重建
        if concept_type != "dimension":
            return None
        dimensions = list(plan.get("dimensions") or [])
        if new_field not in dimensions:
            plan["dimensions"] = dimensions + [new_field]
        concepts = dict(concepts)
        concepts[mention] = {
            "field": new_field,
            "table": str(selected_resolution.get("selected_table") or ""),
            "source": "explicit_user",
            "concept_type": concept_type,
        }
        plan["concept_resolutions"] = concepts
        plan["status"] = "draft"
        plan.pop("locked_at", None)
        plan.pop("confirmed_at", None)
        return plan
    concepts = dict(concepts)
    concepts[mention] = {
        "field": new_field,
        "table": str(selected_resolution.get("selected_table") or ""),
        "source": "explicit_user",
        "concept_type": concept_type,
    }
    plan["concept_resolutions"] = concepts
    # 按概念类型分流：替换目标列表中的旧字段；维度字段缺失时追加保证不丢失
    if old_field in (plan.get(target_key) or []):
        plan[target_key] = [
            new_field if m == old_field else m
            for m in (plan.get(target_key) or [])
        ]
    elif concept_type == "dimension":
        dimensions = list(plan.get("dimensions") or [])
        if new_field not in dimensions:
            plan["dimensions"] = dimensions + [new_field]
    if old_field in (plan.get("fields") or []):
        plan["fields"] = [
            new_field if f == old_field else f
            for f in (plan.get("fields") or [])
        ]
    # order_by 中的排序字段同步替换
    order_by = list(plan.get("order_by") or [])
    replaced_order = False
    for item in order_by:
        if isinstance(item, dict) and item.get("field") == old_field:
            item["field"] = new_field
            replaced_order = True
    if replaced_order:
        plan["order_by"] = order_by
    # 改选后旧方案锁定/确认状态失效，回到草稿阶段
    plan["status"] = "draft"
    plan.pop("locked_at", None)
    plan.pop("confirmed_at", None)
    return plan


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

            # ── 语义层优先匹配：gap >= 阈值时跳过 FAISS 召回，直接使用语义层推荐的来源 ──
            # 去 Topic 化：语义层匹配用本轮输入（完整历史在 history_context 中）
            metric_search_text = current_user_input
            semantic_matches = match_metrics_from_query(metric_search_text, limit=5)
            metric_context_text = format_metric_context(semantic_matches)
            # 语义层唯一命中：按置信度判定（对齐 skill 置信度规则），
            # 完全相等/近义前缀（confidence>=0.9）才短路跳过 FAISS 双路召回；
            # 一般子串命中（0.55~0.9）视为部分匹配，保留候选发现与澄清
            _top_confidence = (
                float(semantic_matches[0].get("confidence", 0) or 0)
                if semantic_matches else 0.0
            )
            _semantic_unique = (
                len(semantic_matches) == 1 and _top_confidence >= 0.9
            ) or (
                len(semantic_matches) >= 2
                and _top_confidence >= 0.9
                and (
                    float(semantic_matches[0].get("score", 0) or 0)
                    - float(semantic_matches[1].get("score", 0) or 0)
                ) >= SEMANTIC_UNIQUE_GAP_THRESHOLD
            )
            # 语义层命中日志：记录每个指标的分数/置信度与短路判定，
            # 便于排查"走了语义层还是召回"
            log_metric_event(
                "semantic.match",
                node_name="planner",
                mention=metric_search_text[:100],
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
            )

            # 语义层唯一命中时跳过 FAISS 双路召回，直接用语义层推荐的来源构建元数据
            if _semantic_unique:
                # 未走检索：置空后续日志/统计引用的检索结果，避免引用未定义变量
                table_docs_with_scores = []
                column_docs_with_scores = []
                winner = semantic_matches[0]
                winner_table = winner.get("source_model", "")
                metadata_lines = [
                    f"[表 {winner_table}, 相似度: 1.0000]\n语义层推荐口径：{winner.get('name', '')}，"
                    f"来源表：{winner_table}，表达式：{winner.get('expression', winner.get('id', ''))}"
                ]
                metadata_context = "\n".join(metadata_lines)
                table_candidates = [{
                    "table": winner_table,
                    "score": 1.0,
                    "comment": f"语义层推荐：{winner.get('name', '')}"
                }]
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
            history_context = _build_history_context(state.get("messages") or [])
            metadata_section = (
                METADATA_SECTION_TEMPLATE.format(metadata_context=metadata_context)
                if metadata_context.strip() and not _semantic_unique
                else ""
            )
            sections = [f"【当前需求基线】\n{current_user_input}"]
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
            accept_locked_plan = planner_output.accept_locked_plan

            # ── 信任 LLM 的 completeness 判定，不做覆盖 ──
            tables = planner_output.tables
            fields = planner_output.fields
            completeness = planner_output.completeness

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

            # 只有用户接受完整 locked 方案并通过程序校验后才能进入 Seeker
            updated_plan = None

            if accept_locked_plan:
                try:
                    updated_plan = confirm_query_plan(confirmed_plan)

                    # 最终确认必须严格使用 locked 方案，禁止检索结果覆盖方案
                    tables = updated_plan.get("tables", [])
                    fields = updated_plan.get("fields", [])
                    completeness = "full"
                    route = "seeker"
                    planner_reason = (
                            "用户接受完整 locked 方案，程序校验通过："
                            + planner_output.reason
                    )
                except ValueError as error:
                    # 即使模型误判为确认，程序校验失败也不能进入 Seeker
                    accept_locked_plan = False
                    route = "advisor"
                    planner_reason = (
                        f"最终确认未通过程序校验：{error}；"
                        "返回 Advisor 继续澄清"
                    )

            elif completeness in ("none", "partial"):
                route = "advisor"
                planner_reason = (
                        f"LLM 判定元数据映射为 {completeness}："
                        + planner_output.reason
                )
            else:
                # 需求已经明确，但还需要 Advisor 生成并展示 locked 方案
                route = "advisor"
                planner_reason = (
                        "需求映射明确，交由 Advisor 生成并展示完整 locked 方案："
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
                "filters": "",
                "complex": planner_output.complex,
                                "completeness": completeness,
                "table_candidates": table_candidates,
                "column_candidates": column_candidates,
                "follow_up_mode": planner_output.follow_up_mode,
            }

            log_node_end(
                "planner",
                route=route,
                accept_locked_plan=accept_locked_plan,
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

            log_sub_info(f"follow_up_mode: {planner_output.follow_up_mode}", node_name="planner")

                        # 节点完成后记录 State 分层摘要，供 trace 查看数据流转
            log_state_snapshot("planner", {**state, **return_value})

            return return_value


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
