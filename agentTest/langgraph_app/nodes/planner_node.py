# Planner 调度节点：FAISS 检索增强元数据 → LLM 结构化解析 → 模糊度判定
#
# 三步流程（对齐论文 SQL-MARS 的 Planner 设计）：
#   ① FAISS 检索：用余弦相似度召回 top-5 增强元数据，拿到每条的余弦距离
#   ② LLM 解析：将召回的元数据 + 用户问题传给 LLM，LLM 输出结构化结果
#      （哪些表、哪些字段、映射完整度 full/partial/none）
#   ③ 阈值判定：LLM 说"none"→ advisor；FAISS 高相似候选 > 3 → advisor；否则 → seeker
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
    MIN_COLUMN_SIMILARITY,
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
    # 与项目 LLM 类指向同一个百炼端点，但 ChatOpenAI 支持 with_structured_output
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
        timer = start_timer()

        log_node_start("planner", question=question)

        try:
            # ── 步骤①：FAISS 检索增强元数据 ──
            # similarity_search_with_score 返回 [(Document, cosine_distance), ...]

            # 表层：拿候选表 + 余弦距离（用于阈值判定）
            table_docs_with_scores = table_vector_store.similarity_search_with_score(question, k=TABLE_SEARCH_K)
            # 字段层：拿候选字段（不限表，LLM 自行筛选归属）
            column_docs_with_scores = column_vector_store.similarity_search_with_score(question, k=COLUMN_SEARCH_K)

            # 拼接元数据上下文：表层在前，字段层在后
            metadata_lines = []
            for doc, score in table_docs_with_scores:
                content = doc.page_content[:500]
                table_name = doc.metadata.get("table", "")
                metadata_lines.append(f"[表: {table_name}, 距离: {score:.4f}]\n{content}")
            for doc, score in column_docs_with_scores:
                content = doc.page_content[:300]
                metadata_lines.append(f"[字段]\n{content}")
            metadata_context = "\n\n".join(metadata_lines)

            # ── 步骤②：LLM 结构化解析 ──
            # 将元数据上下文 + 用户问题一起发给 LLM
            prompt_value = prompt.invoke({
                "question": question,
                "metadata_context": metadata_context,
            })
            # LLM 返回 JSON → LangChain 自动校验并解析为 PlannerOutput 实例
            # planner_output.tables / .fields / .completeness / .reason 可直接访问
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

            # ── 步骤③：阈值判定 ──
            # 余弦距离转 0~1 相似度：similarity = 1 - distance / 2
            # 距离 0.0  → 相似度 1.0（完全一致）
            # 距离 0.2  → 相似度 0.9（论文阈值等价）
            # 距离 1.0  → 相似度 0.5
            high_similarity_count = 0 # 表和字段相似度满足要求的数量
            all_candidates = table_docs_with_scores + column_docs_with_scores
            for doc, score in all_candidates:
                similarity = 1 - score / 2
                if similarity > HIGH_SIMILARITY_THRESHOLD:  # 等价于论文的 "相似度 > 0.8"
                    high_similarity_count += 1

            # 取表级和字段级最高相似度
            top_table_similarity = max(
                (1 - score / 2 for doc, score in table_docs_with_scores),
                default=0
            )
            top_column_similarity = max(
                (1 - score / 2 for doc, score in column_docs_with_scores),
                default=0
            )

            if planner_output.completeness == "none":
                route = "advisor"
                planner_reason = "LLM 判定无法映射到任何表: " + planner_output.reason
            elif top_table_similarity < MIN_COLUMN_SIMILARITY:
                route = "advisor"
                planner_reason = f"表级最佳匹配相似度过低({top_table_similarity:.2f} < {MIN_COLUMN_SIMILARITY}): {planner_output.reason}"
            elif top_column_similarity < MIN_TABLE_SIMILARITY:
                # 表找到了但字段匹配度太低 → advisor 展示候选字段让用户选
                route = "advisor"
                planner_reason = f"字段级最佳匹配相似度过低({top_column_similarity:.2f} < {MIN_TABLE_SIMILARITY}): {planner_output.reason}"
            elif high_similarity_count > MAX_HIGH_SIMILARITY_COUNT:
                route = "advisor"
                planner_reason = f"高相似度候选过多({high_similarity_count}个 > {MAX_HIGH_SIMILARITY_COUNT})，存在歧义: {planner_output.reason}"
            else:
                route = "seeker"
                planner_reason = planner_output.reason

            log_node_end(
                "planner",
                route=route,
                completeness=planner_output.completeness,
                planner_reason=planner_reason,
                tables=planner_output.tables,
                fields=planner_output.fields,
                high_similarity_count=high_similarity_count,
                table_top_scores=table_scores_str,  # 召回的表
                column_top_scores=column_scores_str,  # 召回的字段
                duration_ms=elapsed_ms(timer),
            )

            # 返回三个字段写入 AgentState，供路由器读取
            return {
                "route": route,
                "planner_reason": planner_reason,
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