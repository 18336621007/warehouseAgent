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
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, log_node_event
from agentTest.langgraph_app.runtime.graph_logger import log_example_retrieved
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.config.advisor import PER_TABLE_COLUMN_QUOTA
from agentTest.config.planner import (
    TABLE_SEARCH_K,
    COLUMN_SEARCH_K,
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


def _build_pending_options_text(pendings, resolutions=None):
    """把待澄清候选组装成精简文本，供 Planner 判断用户选择。

    pending 已关闭（用户已完成选择）时，回退展示已确认概念的候选快照，
    让“改成第一个/第二个”等改选指代有稳定参照，不依赖模型记忆。
    """
    lines = []
    for pending in pendings or []:
        lines.append(f"[{pending.get('mention', '')} id={pending.get('clarification_id', '')}]")
        for option in pending.get("options") or []:
            lines.append(
                f"{option.get('index')}. {option.get('meaning', '')}（字段：{option.get('field', '')}）"
            )
    if not lines:
        for resolution in (resolutions or []):
            if resolution.get("status") != "resolved":
                continue
            candidates = resolution.get("candidates") or []
            if len(candidates) <= 1:
                continue
            lines.append(f"[{resolution.get('mention', '')} 历史展示候选（改选时参考）]")
            for index, candidate in enumerate(candidates, 1):
                field = candidate.get("field", "")
                table = str(candidate.get("table") or "").split(".")[-1]
                lines.append(f"{index}. {field}（表：{table}）")
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
        # 方案尚未记录该概念解析：无法确定旧字段位置，交由 Advisor 重建
        return None
    concepts = dict(concepts)
    concepts[mention] = {
        "field": new_field,
        "table": str(selected_resolution.get("selected_table") or ""),
        "source": "explicit_user",
    }
    plan["concept_resolutions"] = concepts
    # measures/fields 中替换该概念旧字段
    if old_field in (plan.get("measures") or []):
        plan["measures"] = [
            new_field if m == old_field else m
            for m in (plan.get("measures") or [])
        ]
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

    # ChatOpenAI：LangChain 标准的 OpenAI 兼容客户端
    chat_openai = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,                   # 判定任务不需要随机性
        extra_body=get_model_extra_body(),
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
            table_docs_with_scores = table_vector_store.similarity_search_with_score(retrieval_question, k=TABLE_SEARCH_K)
            table_scope = _build_table_scope(table_docs_with_scores)
            column_docs_with_scores = _recall_columns(column_vector_store, retrieval_question, table_scope)

            # 拼接元数据上下文：表层在前，字段层在后
            metadata_lines = []
            for doc, score in table_docs_with_scores:
                content = doc.page_content[:500]
                table_name = doc.metadata.get("table", "")
                metadata_lines.append(f"[表 {table_name}, 距离: {score:.4f}]\n{content}")
            for doc, score in column_docs_with_scores:
                content = doc.page_content[:300]
                metadata_lines.append(f"[字段]\n{content}")
            metadata_context = "\n\n".join(metadata_lines)

            # 构建候选池（带分数+注释），供 Advisor 精排使用
            table_candidates = []
            for doc, score in table_docs_with_scores:
                table_candidates.append({
                    "table": doc.metadata.get("table", ""),
                    "score": float(round(1 - float(score) / 2, 4)),
                    "comment": (doc.page_content or "")[:200]
                })
            column_candidates = []
            for doc, score in column_docs_with_scores:
                column_candidates.append({
                    "table": doc.metadata.get("table", ""),
                    "field": doc.metadata.get("field", doc.metadata.get("column", "")),
                    "score": float(round(1 - float(score) / 2, 4)),
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
            prompt_value = prompt.invoke({
                "question": original_question,
                "current_user_input": current_user_input,
                "metadata_context": metadata_context,
                "example_context": example_context,
                "confirmed_context": confirmed_context,
                "history_context": _build_history_context(state.get("messages") or []),
                "pending_options": _build_pending_options_text(
                    (state.get("analysis_spec") or {}).get("pending_clarifications") or [],
                    (state.get("analysis_spec") or {}).get("metric_resolutions") or [],
                ),
                "resolution_context": MetricClarificationService.build_resolution_context(
                    state.get("analysis_spec") or {}
                ),
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
            table_scores = []
            for doc, score in table_docs_with_scores:
                similarity = round(1 - score / 2, 3)
                name = doc.metadata.get("table", "?")
                short_name = name if len(name) <= 40 else "..." + name[-37:]
                table_scores.append(f"{short_name}({similarity})")
            table_scores_str = " | ".join(table_scores)

            column_scores = []
            for doc, score in column_docs_with_scores:
                similarity = round(1 - score / 2, 3)
                name = doc.metadata.get("column", "?")
                column_scores.append(f"{name}({similarity})")
            column_scores_str = " | ".join(column_scores)

            # ── 步骤③：基于完整有效需求统计各层高相似度候选数量 ──
            high_similarity_table_count = 0
            for doc, score in ambiguity_table_docs_with_scores:
                similarity = 1 - score / 2
                if similarity > HIGH_SIMILARITY_THRESHOLD:
                    high_similarity_table_count += 1

            high_similarity_column_count = 0
            selected_tables = set(tables)

            for doc, score in ambiguity_column_docs_with_scores:
                # 字段歧义只在 Planner 已确定的目标表内统计
                document_table = doc.metadata.get("table", "")
                if selected_tables and document_table not in selected_tables:
                    continue

                similarity = 1 - score / 2
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
            log_sub_info(f"有效需求: {effective_query}", node_name="planner")
            log_sub_info(f"表: {table_scores_str}", node_name="planner")
            log_sub_info(f"字段: {column_scores_str}", node_name="planner")

            # Planner 路由结果决定 Topic 下一阶段
            if route == "seeker":
                next_topic_status = "confirmed"
            else:
                next_topic_status = "clarifying"


            # 以下这段代码让 AnalysisSpec 从“每轮被 LLM 覆盖重建”变成“增量更新”：
            # 模型判断用户选择（PlannerOutput.user_selection），程序只做白名单校验（validate_user_selection），
            # 再保留上轮 resolved 证据，只补新概念的 ambiguous 记录，并按概念是否存活正确清理/保留 pending，
            # 从状态层面消除“模型理解了、State 却还停在 ambiguous”导致的重复确认循环。
            # ── 组装 AnalysisSpec：模型判断 + 程序白名单校验 + pending 生命周期 ──

            # 1. 读取上轮状态，建立索引
            existing_spec = state.get("analysis_spec") or {}
            existing_resolutions = existing_spec.get("metric_resolutions") or []
            resolution_by_mention = {
                item.get("mention", ""): item
                for item in existing_resolutions
                if isinstance(item, dict) and item.get("mention")
            }
            pending_clarifications = list(existing_spec.get("pending_clarifications") or [])
            open_pending = next(
                (p for p in pending_clarifications if p.get("status") == "open"),
                None,
            )

            # 2. 模型判断用户选择，程序白名单校验（判断归模型，校验归程序）
            # 不要求存在 open pending：用户改选、补充选择时同样生效，
            # 白名单 = pending 候选 ∪ 上轮已确认字段 ∪ 本轮召回候选字段
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
                    pending_clarifications,
                    existing_resolutions,
                    candidate_fields,
                    list(planner_output.metric_mentions or []),
                )
                if selected_resolution is None:
                    # 模型判断了选择但程序白名单未命中：记录证据，便于排查改选失败
                    log_sub_info(
                        "user_selection未命中: "
                        f"field={user_selection.get('field', '')} "
                        f"reasoning={str(user_selection.get('reasoning', ''))[:120]}",
                        node_name="planner",
                    )

            # 3. 决定本轮的 metric_mentions
            # 只保留 LLM 输出的当前需求概念：上轮 resolved 概念不再自动追加，
            # 用户改选/放弃后旧概念从概念集消失，避免“纯新用户的新增订单”这类
            # 旧口径因跨轮保留而复活；用户明确提到的概念由 LLM 自然输出。
            metric_mentions = list(planner_output.metric_mentions or [])
            if not metric_mentions:
                metric_mentions = list(existing_spec.get("metric_mentions") or [])
            if selected_resolution is not None:
                # 白名单校验通过：用户选定单一口径时确保该概念在集合中，
                # 其余概念以 LLM 输出为准，不自动补回已放弃概念。
                selected_mention = selected_resolution.get("mention", "")
                resolution_by_mention[selected_mention] = selected_resolution
                if selected_mention and selected_mention not in metric_mentions:
                    metric_mentions.append(selected_mention)


            # 4. 重建 metric_resolutions（只保留程序可信的解析证据）
            # llm_submitted 只是模型单轮解读，不跨轮保留，避免改选后旧口径残留；
            # 用户明确选择(explicit_user)或元数据唯一(unique_metadata)的解析可保留。
            new_resolutions = []
            for mention in metric_mentions:
                existing = resolution_by_mention.get(mention)
                if (
                    existing
                    and existing.get("status") == "resolved"
                    and existing.get("resolution_source") in ("explicit_user", "unique_metadata")
                ):
                    new_resolutions.append(existing)
                else:
                    new_resolutions.append({
                        "mention": mention,
                        "concept_type": "metric",
                        "status": "ambiguous",
                        "selected_field": "",
                        "selected_table": "",
                        "resolution_source": "",
                        "candidates": [],
                    })

            # 5. 组装新 AnalysisSpec + 管理 pending 生命周期
            analysis_spec = dict(existing_spec)
            analysis_spec.update({
                "analysis_type": planner_output.analysis_type,
                "metric_mentions": metric_mentions,
                "dimension_mentions": list(planner_output.dimension_mentions or []),
                "time_range": "",
                "time_grain": "",
                "filters": [],
                "order_by": [],
                "limit": 0,
                "comparison": {},
                "metric_resolutions": new_resolutions,
            })
            if selected_resolution is not None:
                # 选择已通过白名单校验：关闭对应 pending（按澄清 ID），解析证据保留在 metric_resolutions
                resolved_cid = selected_resolution.get("clarification_id", "")
                analysis_spec["pending_clarifications"] = [
                    p for p in pending_clarifications
                    if p.get("clarification_id") != resolved_cid
                ]
            elif open_pending and open_pending.get("mention") in metric_mentions:
                # 概念仍存活且用户未选择：保留 open pending（延迟澄清恢复）
                analysis_spec["pending_clarifications"] = pending_clarifications
            else:
                # 概念已不在需求中（换话题/推翻）：清理 pending
                analysis_spec["pending_clarifications"] = []

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

            return return_value


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
