# Planner 调度节点：FAISS 检索增强元数据 → LLM 结构化解析 → 模糊度判定
#
# Planner 是唯一的调度中心：LLM 语义判断为主，FAISS 仅做 full 时的极端兜底。
# 三步流程（对齐论文 SQL-MARS 的 Planner 设计）：
#   ① FAISS 检索：用余弦相似度召回 top-k 增强元数据
#   ② LLM 解析：将召回元数据 + 用户问题 + 用户实际输入 + 已确认方案 + Advisor 上轮回复传给 LLM
#   ③ 阈值判定：模糊需求进入 Advisor；明确需求先由 Advisor 锁定方案；
#       只有用户最终确认 locked 方案后才能进入 Seeker
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from agentTest.langgraph_app.services.query_plan_service import confirm_query_plan
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, log_node_event
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.config.planner import (
    TABLE_SEARCH_K,
    COLUMN_SEARCH_K,
    HIGH_SIMILARITY_THRESHOLD,
    MAX_HIGH_SIMILARITY_COUNT
)
from agentTest.langgraph_app.message_utils import get_last_ai_content



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


        # ── 读取独立的 confirmed_plan（Advisor 写入，Planner 只读不改）──
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

            # ── 步骤①：FAISS 检索增强元数据 ──
            table_docs_with_scores = table_vector_store.similarity_search_with_score(retrieval_question, k=TABLE_SEARCH_K)
            column_docs_with_scores = column_vector_store.similarity_search_with_score(retrieval_question, k=COLUMN_SEARCH_K)

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
                    log_node_event("planner", f"优秀示例检索: 命中{len(examples)}条, top_sim={sim_val}, q={top_q}")


            # ── 步骤②：LLM 结构化解析（含 current_user_input、confirmed_context、advisor_last_answer）──
            prompt_value = prompt.invoke({
                "question": original_question,
                "current_user_input": current_user_input,
                "metadata_context": metadata_context,
                "example_context": example_context,
                "confirmed_context": confirmed_context,
                "advisor_last_answer": advisor_last_answer,
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

            ambiguity_column_docs_with_scores = (
                column_vector_store.similarity_search_with_score(
                    effective_query,
                    k=COLUMN_SEARCH_K,
                )
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
            log_sub_info(f"有效需求: {effective_query}")
            log_sub_info(f"表: {table_scores_str}")
            log_sub_info(f"字段: {column_scores_str}")

            # Planner 路由结果决定 Topic 下一阶段
            if route == "seeker":
                next_topic_status = "confirmed"
            else:
                next_topic_status = "clarifying"

            # ── 组装 AnalysisSpec：提取业务概念，不替用户选择物理指标 ──
            existing_spec = state.get("analysis_spec") or {}
            existing_resolutions = existing_spec.get("metric_resolutions") or []
            resolution_by_mention = {
                item.get("mention", ""): item
                for item in existing_resolutions
                if isinstance(item, dict) and item.get("mention")
            }
            metric_mentions = list(planner_output.metric_mentions or [])
            if not metric_mentions:
                metric_mentions = existing_spec.get("metric_mentions") or []

            new_resolutions = []
            for mention in metric_mentions:
                if mention in resolution_by_mention:
                    # 保留 Advisor 已解析记录，避免用户选择被 Planner 重置
                    new_resolutions.append(resolution_by_mention[mention])
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

            analysis_spec = {
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
            }

            return_value = {
                "route": route,
                "planner_reason": planner_reason,
                "original_question": original_question,
                "planner_entities": new_entities,
                "topic_status": next_topic_status,
                "analysis_spec": analysis_spec,
            }
            # 用户最终确认后，写回 status=confirmed 的查询方案
            if updated_plan is not None:
                return_value["confirmed_plan"] = updated_plan

            return return_value


        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
