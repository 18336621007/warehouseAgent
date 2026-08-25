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
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, log_node_event
from agentTest.langgraph_app.runtime.graph_logger import log_example_retrieved
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
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
from agentTest.langgraph_app.message_utils import get_last_ai_content


def _build_history_context(messages, max_turns=10, max_chars_per_msg=500):
    """把最近几轮 Human/AI 消息组装成对话历史文本，排除工具消息。"""
    lines = []
    for msg in (messages or [])[-max_turns * 2:]:
        name = getattr(msg, "name", "") or ""
        if isinstance(msg, HumanMessage):
            role = "用户"
        else:
            role = f"助手({name})" if name else "助手"
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

        # Topic 首轮使用当前输入初始化原始问题，后续轮次读取 checkpoint
        original_question = state.get("original_question") or current_user_input


        # ── 读取共享方案 confirmed_plan（Advisor 写入草稿/locked；用户选择/改选时 Planner 改写草稿）──
        confirmed_plan = state.get("confirmed_plan") or {}
        has_plan = bool(
            confirmed_plan.get("table")
            or confirmed_plan.get("tables")
        )
        if has_plan:
            plan_table = confirmed_plan.get("table", "")
            plan_measures = confirmed_plan.get("measures") or []
            plan_dimensions = confirmed_plan.get("dimensions") or []
            plan_fields = confirmed_plan.get("fields") or []
            plan_time_field = confirmed_plan.get("time_field", "")
            plan_time_range = confirmed_plan.get("time_range", "")
            plan_filters = confirmed_plan.get("filters", "")
            plan_status = confirmed_plan.get("status", "")

            confirmed_context = (
                f"方案状态: {plan_status or '未设置'}\n"
                f"数据表: {plan_table or '未设置'}\n"
                f"度量字段: {', '.join(plan_measures) or '无'}\n"
                f"维度字段: {', '.join(plan_dimensions) or '无'}\n"
                f"全部字段: {', '.join(plan_fields) or '无'}\n"
                f"时间字段: {plan_time_field or '未设置'}\n"
                f"时间范围: {plan_time_range or '未设置'}\n"
                f"过滤条件: {plan_filters or '无'}"
            )
        else:
            confirmed_context = "当前尚未形成查询方案。"


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
        log_node_start("planner", question=original_question)

        try:
            # 将对话上下文组合成检索文本，使“1”“A”等简短回答具有候选语义
            retrieval_parts = [
                f"原始查数需求：{original_question}",
            ]

            if has_plan:
                plan_table = confirmed_plan.get("table", "")
                plan_fields = confirmed_plan.get("fields") or []

                retrieval_parts.append(
                    "当前方案："
                    f"表={plan_table or '未确定'}，"
                    f"字段={', '.join(plan_fields) or '未确定'}"
                )

            if advisor_last_answer:
                retrieval_parts.append(
                    f"Advisor上轮候选或方案：{advisor_last_answer}"
                )

            if current_user_input.strip():
                retrieval_parts.append(
                    f"用户本轮回答：{current_user_input}"
                )

            retrieval_question = "\n".join(retrieval_parts)

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
                            # BM25 分数归一化到与向量可比范围（向量余弦相似度约 0.3-1.0），乘以向量最高分作为锚点
                            max_vec_score = table_docs_with_scores[0][1] if table_docs_with_scores else 1.0
                            normalized_score = bm25_score / (bm25_score + 1.0) * max_vec_score
                            table_docs_with_scores.append((doc, normalized_score))
                except Exception:
                    pass  # BM25 异常不影响主流程
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

            # ── 新增：检索历史优质示例，辅助模糊度判定 ──
            example_vs = runtime.get("example_vector_store")
            example_context = ""
            if example_vs:
                examples = example_vs.search_similar(original_question, k=2)
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


            # ── 步骤②：LLM 结构化解析（含 current_user_input、confirmed_context、advisor_last_answer）──
            # 语义层指标上下文：用 metric_matcher 从用户原始问题匹配候选指标，
            # 让 LLM 优先参考权威 source_model / expression / aliases，而不是凭空猜测表/字段。
            # 这里使用 original_question + current_user_input 拼接作为匹配源，
            # 避免依赖后续 LLM 输出的 effective_query。
            metric_search_text = "\n".join(
                part for part in (original_question, current_user_input) if part and part.strip()
            )
            metric_context_text = format_metric_context(
                match_metrics_from_query(metric_search_text, limit=5)
            )
            prompt_value = prompt.invoke({
                "question": original_question,
                "current_user_input": current_user_input,
                "metadata_context": metadata_context,
                "example_context": example_context,
                "confirmed_context": confirmed_context,
                "history_context": _build_history_context(state.get("messages") or []),
                "recent_candidates_text": _build_recent_candidates_text(
                    (state.get("analysis_spec") or {}).get("recent_shown_candidates") or [],
                    list((state.get("analysis_spec") or {}).get("metric_resolutions") or [])
                    + list((state.get("analysis_spec") or {}).get("dimension_resolutions") or []),
                ),
                "resolution_context": MetricClarificationService.build_resolution_context(
                    state.get("analysis_spec") or {}
                ),
                "metric_context": metric_context_text,
            })
            planner_output = structured_llm.invoke(prompt_value)

            effective_query = (
                    planner_output.effective_query.strip()
                    or original_question
            )
            accept_locked_plan = planner_output.accept_locked_plan

            # ── 信任 LLM 的 completeness 判定，不做覆盖 ──
            tables = planner_output.tables
            fields = planner_output.fields
            completeness = planner_output.completeness

            # 使用 LLM 还原后的完整需求重新探测候选数量，避免“1”“A”等短回答携带整组选项
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


            # 以下这段代码让 AnalysisSpec 从“每轮被 LLM 覆盖重建”变成“增量更新”：
            # 模型判断用户选择（PlannerOutput.user_selection），程序只做白名单校验（validate_user_selection），
            # 再保留上轮 resolved 证据，只补新概念的 ambiguous 记录；最近展示候选由 Advisor 写回，
            # 从状态层面消除“模型理解了、State 却还停在 ambiguous”导致的重复确认循环。
            # ── 组装 AnalysisSpec：模型判断 + 程序白名单校验 + 最近展示候选快照 ──

            # 1. 读取上轮状态，建立索引（指标与维度解析证据合并反查，支持维度改选）
            existing_spec = state.get("analysis_spec") or {}
            existing_resolutions = (
                list(existing_spec.get("metric_resolutions") or [])
                + list(existing_spec.get("dimension_resolutions") or [])
            )
            resolution_by_mention = {
                item.get("mention", ""): item
                for item in existing_resolutions
                if isinstance(item, dict) and item.get("mention")
            }
            recent_shown_candidates = list(existing_spec.get("recent_shown_candidates") or [])

            # 2. 模型判断用户选择，程序白名单校验（判断归模型，校验归程序）
            # 不要求存在最近展示候选：用户改选、补充选择时同样生效，
            # 白名单 = 最近展示候选 ∪ 上轮已确认字段 ∪ 本轮召回候选字段
            selected_resolution = None
            user_selection = (
                planner_output.user_selection.model_dump()
                if planner_output.user_selection
                else {}
            )
            if user_selection.get("selected"):
                candidate_fields = [
                    str(candidate.get("field") or "")
                    for candidate in (new_entities.get("column_candidates") or [])
                    if candidate.get("field")
                ] + list(planner_output.fields or [])
                selected_resolution = MetricClarificationService.validate_user_selection(
                    user_selection,
                    recent_shown_candidates,
                    existing_resolutions,
                    candidate_fields,
                    list(planner_output.metric_mentions or []),
                    list(planner_output.dimension_mentions or []),
                    list(new_entities.get("column_candidates") or []),
                )
                if selected_resolution is None:
                    # 模型判断了选择但程序白名单未命中：记录证据，便于排查改选失败
                    log_sub_info(
                        "user_selection未命中: "
                        f"field={user_selection.get('field', '')} "
                        f"mention={user_selection.get('mention', '')} "
                        f"reasoning={str(user_selection.get('reasoning', ''))[:120]}",
                        node_name="planner",
                    )

            # 3. 决定本轮的 metric_mentions / dimension_mentions
            # 只保留 LLM 输出的当前需求概念：上轮 resolved 概念不再自动追加，
            # 用户改选/放弃后旧概念从概念集消失，避免“纯新用户的新增订单”这类
            # 旧口径因跨轮保留而复活；用户明确提到的概念由 LLM 自然输出。
            metric_mentions = list(planner_output.metric_mentions or [])
            if not metric_mentions:
                metric_mentions = list(existing_spec.get("metric_mentions") or [])
            dimension_mentions = list(planner_output.dimension_mentions or [])
            if not dimension_mentions:
                dimension_mentions = list(existing_spec.get("dimension_mentions") or [])
            if selected_resolution is not None:
                # 白名单校验通过：用户选定单一口径时确保该概念在对应类型集合中，
                # 其余概念以 LLM 输出为准，不自动补回已放弃概念。
                selected_mention = selected_resolution.get("mention", "")
                resolution_by_mention[selected_mention] = selected_resolution
                selected_concept_type = selected_resolution.get("concept_type") or "metric"
                if selected_mention:
                    if selected_concept_type == "dimension":
                        if selected_mention not in dimension_mentions:
                            dimension_mentions.append(selected_mention)
                    elif selected_mention not in metric_mentions:
                        metric_mentions.append(selected_mention)


            # 4. 重建解析证据（指标/维度分别落盘，只保留程序可信的解析）
            # llm_submitted 只是模型单轮解读，不跨轮保留，避免改选后旧口径残留；
            # 用户明确选择(explicit_user)或元数据唯一(unique_metadata)的解析可保留。
            def _rebuild_resolutions(mentions: list[str], concept_type: str) -> list[dict]:
                rebuilt = []
                for mention in mentions:
                    existing = resolution_by_mention.get(mention)
                    if (
                        existing
                        and existing.get("status") == "resolved"
                        and existing.get("resolution_source") in ("explicit_user", "unique_metadata")
                    ):
                        rebuilt.append(existing)
                    else:
                        rebuilt.append({
                            "mention": mention,
                            "concept_type": concept_type,
                            "status": "ambiguous",
                            "selected_field": "",
                            "selected_table": "",
                            "resolution_source": "",
                            "candidates": [],
                        })
                return rebuilt

            new_metric_resolutions = _rebuild_resolutions(metric_mentions, "metric")
            new_dimension_resolutions = _rebuild_resolutions(dimension_mentions, "dimension")

            # 5. 组装新 AnalysisSpec：最近展示候选快照跨轮保留，由 Advisor 负责更新
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
                "metric_resolutions": new_metric_resolutions,
                "dimension_resolutions": new_dimension_resolutions,
                "recent_shown_candidates": recent_shown_candidates,
            })

            # 6. 写回 State（首轮记录话题原文；每轮更新改写后的有效需求）
            return_value = {
                "route": route,
                "planner_reason": planner_reason,
                "effective_query": effective_query,
                "planner_entities": new_entities,
                "topic_status": next_topic_status,
                "analysis_spec": analysis_spec,
                "follow_up_mode": planner_output.follow_up_mode,  # 供 web 层决定是否切 Topic
            }
            # 首轮写入话题原始问题（后续轮保留原文，不覆盖）
            if not state.get("original_question"):
                return_value["original_question"] = current_user_input
            # 用户最终确认后，写回 status=confirmed 的查询方案
            if updated_plan is not None:
                return_value["confirmed_plan"] = updated_plan

            # 用户选择/改选口径后，Planner 将选择落到共享方案草稿
            # （替换该概念旧字段，状态回到 draft；方案尚无该概念时交由 Advisor 重建）
            if selected_resolution is not None:
                selection_plan = _apply_user_selection_to_draft(
                    state.get("confirmed_plan") or {},
                    selected_resolution,
                )
                if selection_plan is not None:
                    return_value["confirmed_plan"] = selection_plan
                    log_sub_info(
                        f"口径选择已落方案草稿: {selected_resolution.get('selected_field', '')}",
                        node_name="planner",
                    )

            if selected_resolution is not None:
                log_sub_info(
                    f"用户选择校验: 命中 {selected_resolution.get('selected_field', '')}",
                    node_name="planner",
                )
            log_sub_info(f"follow_up_mode: {planner_output.follow_up_mode}", node_name="planner")

            # 节点完成后记录 State 分层摘要，供 trace 查看数据流转
            log_state_snapshot("planner", {**state, **return_value})

            return return_value


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
