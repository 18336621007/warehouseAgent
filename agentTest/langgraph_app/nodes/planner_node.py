# Planner 调度节点：FAISS 检索增强元数据 → LLM 结构化解析 → 模糊度判定
#
# Planner 是唯一的调度中心：LLM 语义判断 + FAISS 向量校验，双向验证。
# 判定优先级：LLM 为主，FAISS 为辅。
# 三步流程（对齐论文 SQL-MARS 的 Planner 设计）：
#   ① FAISS 检索：用余弦相似度召回 top-k 增强元数据
#   ② LLM 解析：将召回元数据 + 用户问题传给 LLM，输出结构化结果
#   ③ 阈值判定：LLM full + FAISS 不过度异常 → seeker；
#      LLM none → advisor；LLM partial → FAISS 兜底
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.config.planner import (
    MIN_TABLE_SIMILARITY,
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

            # ── 收集 top-k 分数，用于日志分析 ──
            table_scores = []
            for doc, score in table_docs_with_scores:
                similarity = round(1 - score / 2, 3)
                name = doc.metadata.get("table", "?")
                table_scores.append(f"{name}({similarity})")
            table_scores_str = " | ".join(table_scores)

            column_scores = []
            for doc, score in column_docs_with_scores:
                similarity = round(1 - score / 2, 3)
                name = doc.metadata.get("column", "?")
                column_scores.append(f"{name}({similarity})")
            column_scores_str = " | ".join(column_scores)

            # ── 步骤③：阈值判定（LLM + FAISS 双向验证，对齐论文 §3.2.1）──
            #
            # 判定优先级：LLM 为主，FAISS 为辅。
            # - LLM full：FAISS 做极端兜底（候选数 > 3 倍阈值才否决）
            # - LLM none：直接 advisor
            # - LLM partial：FAISS 常规兜底（字段匹配、候选数）

            # FAISS 指标
            top_column_similarity = max(
                (1 - score / 2 for doc, score in column_docs_with_scores),
                default=0
            )
            # 仅统计字段层的高相似候选数（表级候选不构成"字段语义歧义"）
            high_similarity_count = 0
            for doc, score in column_docs_with_scores:
                similarity = 1 - score / 2
                if similarity > HIGH_SIMILARITY_THRESHOLD:  # 0.65
                    high_similarity_count += 1

            if planner_output.completeness == "none":
                # LLM 认为完全无法映射 → advisor
                route = "advisor"
                planner_reason = "LLM 判定无法映射: " + planner_output.reason
            elif planner_output.completeness == "full":
                # LLM 认为映射完整 → 以 LLM 为准，FAISS 仅做极端兜底
                if high_similarity_count > MAX_HIGH_SIMILARITY_COUNT * 3:
                    # 极端情况：字段候选爆炸（如 15+），FAISS 否决 LLM
                    route = "advisor"
                    planner_reason = f"LLM判定full但字段候选过多({high_similarity_count} > {MAX_HIGH_SIMILARITY_COUNT * 3})，存在不确定性: {planner_output.reason}"
                else:
                    route = "seeker"
                    planner_reason = "LLM 判定映射完整: " + planner_output.reason
            elif top_column_similarity < MIN_TABLE_SIMILARITY:
                # partial：LLM 不确定字段 → FAISS 字段匹配兜底
                route = "advisor"
                planner_reason = f"partial且字段级匹配过低({top_column_similarity:.2f} < {MIN_TABLE_SIMILARITY}): {planner_output.reason}"
            elif high_similarity_count > MAX_HIGH_SIMILARITY_COUNT:
                # partial：字段候选过多 → advisor
                route = "advisor"
                planner_reason = f"partial且字段候选过多({high_similarity_count} > {MAX_HIGH_SIMILARITY_COUNT}): {planner_output.reason}"
            else:
                # partial 但 FAISS 校验通过 → seeker
                route = "seeker"
                planner_reason = "partial但FAISS校验通过: " + planner_output.reason

            log_node_end(
                "planner",
                route=route,
                completeness=planner_output.completeness,
                planner_reason=planner_reason,
                tables=planner_output.tables,
                fields=planner_output.fields,
                high_similarity_count=high_similarity_count,
                table_top_scores=table_scores_str,
                column_top_scores=column_scores_str,
                duration_ms=elapsed_ms(timer),
            )

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
            log_node_error("planner", error=error, duration_ms=elapsed_ms(timer))
            raise

    return planner_node
