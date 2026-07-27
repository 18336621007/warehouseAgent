# Planner 调度节点：FAISS 检索增强元数据 → LLM 结构化解析 → 模糊度判定
#
# Planner 是唯一的调度中心：LLM 语义判断为主，FAISS 仅做 full 时的极端兜底。
# 三步流程（对齐论文 SQL-MARS 的 Planner 设计）：
#   ① FAISS 检索：用余弦相似度召回 top-k 增强元数据
#   ② LLM 解析：将召回元数据 + 用户问题传给 LLM，输出结构化结果
#   ③ 阈值判定：LLM full → seeker（FAISS 只做极端否决）；
#      LLM partial/none → advisor
#
# 快速通道：如果 Advisor 已通过 confirm_selection 工具确认了实体
#   → planner_entities.confirmed == True → 跳过 ①②③，直接路由 seeker
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.config.planner import (
    MAX_HIGH_SIMILARITY_COUNT,
    HIGH_SIMILARITY_THRESHOLD,
    TABLE_SEARCH_K,
    COLUMN_SEARCH_K,
)


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
        question = state["question"]

        # 每轮 Planner 都独立判定：original_question 由 demo 层维护
        original_question = state.get("original_question", question)

        # ── 快速通道：Advisor 已通过 confirm_selection 工具确认了实体 ──
        # 此时不再需要 FAISS + LLM 评估，直接路由 seeker
        planner_entities = state.get("planner_entities") or {}
        if planner_entities.get("confirmed"):
            timer = start_timer()
            log_node_start("planner", question=question)
            log_node_end(
                "planner",
                route="seeker",
                completeness="full",
                tables=str(planner_entities.get("tables", [])),
                fields=str(planner_entities.get("fields", [])),
                reason="Advisor confirmed entities, skip evaluation",
                ms=elapsed_ms(timer),
            )
            return {
                "route": "seeker",
                "planner_reason": "Advisor已通过confirm_selection确认实体，跳过评估",
                "original_question": original_question,
                "planner_entities": planner_entities,
            }

        # ── 以下为原有的 FAISS + LLM 评估流程 ──
        timer = start_timer()
        log_node_start("planner", question=question)

        try:
            # ── 步骤①：FAISS 检索增强元数据 ──
            table_docs_with_scores = table_vector_store.similarity_search_with_score(question, k=TABLE_SEARCH_K)
            column_docs_with_scores = column_vector_store.similarity_search_with_score(question, k=COLUMN_SEARCH_K)

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

            # ── 步骤②：LLM 结构化解析 ──
            prompt_value = prompt.invoke({
                "question": question,
                "metadata_context": metadata_context,
            })
            planner_output = structured_llm.invoke(prompt_value)

            # 后校验：不以 LLM 的 completeness 为准，按实际输出重算
            tables = planner_output.tables
            fields = planner_output.fields

            if not tables:
                planner_output.completeness = "none"
            elif not fields:
                planner_output.completeness = "partial"
            else:
                planner_output.completeness = "full"

            # ── 收集 top-k 分数（每条截断到 40 字符），用于辅助日志 ──
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

            # ── 步骤③：阈值判定（LLM 为主，FAISS 仅做 full 时极端兜底）──
            #
            # none / partial → advisor（字段不确定，必须澄清）
            # full → seeker（LLM 唯一确定了字段，信任它）
            #   full 的例外：字段候选爆炸（> 3 倍阈值）→ advisor（FAISS 极端兜底）

            # 统计字段层高相似候选数（仅用于 full 时的极端兜底）
            high_similarity_count = 0
            for doc, score in column_docs_with_scores:
                similarity = 1 - score / 2
                if similarity > HIGH_SIMILARITY_THRESHOLD:
                    high_similarity_count += 1

            if planner_output.completeness in ("none", "partial"):
                # none：无法定位任何表；partial：有表但字段不确定 → advisor
                route = "advisor"
                planner_reason = f"LLM判定{planner_output.completeness}: " + planner_output.reason
            elif high_similarity_count > MAX_HIGH_SIMILARITY_COUNT * 3:
                # full 但字段候选爆炸 → FAISS 否决 LLM（极端兜底）
                route = "advisor"
                planner_reason = f"LLM判定full但字段候选过多({high_similarity_count} > {MAX_HIGH_SIMILARITY_COUNT * 3}): {planner_output.reason}"
            else:
                # full 且候选正常 → trust LLM
                route = "seeker"
                planner_reason = "LLM判定映射完整: " + planner_output.reason

            # ── 日志：主行只放关键决策信息 ──
            log_node_end(
                "planner",
                route=route,
                completeness=planner_output.completeness,
                tables=str(tables),
                fields=str(fields),
                high_sim=high_similarity_count,
                reason=planner_reason,
                ms=elapsed_ms(timer),
            )
            # 辅助行：表/字段检索分数
            log_sub_info(f"表: {table_scores_str}")
            log_sub_info(f"字段: {column_scores_str}")

            return {
                "route": route,
                "planner_reason": planner_reason,
                "original_question": original_question,
                "planner_entities": {
                    "tables": planner_output.tables,
                    "fields": planner_output.fields,
                    "completeness": planner_output.completeness,
                },
            }

        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
