# Planner 调度节点：FAISS 检索增强元数据 → LLM 结构化解析 → 模糊度判定
#
# Planner 是唯一的调度中心：LLM 语义判断为主，FAISS 仅做 full 时的极端兜底。
# 三步流程（对齐论文 SQL-MARS 的 Planner 设计）：
#   ① FAISS 检索：用余弦相似度召回 top-k 增强元数据
#   ② LLM 解析：将召回元数据 + 用户问题 + 用户实际输入 + 已确认方案 + Advisor 上轮回复传给 LLM
#   ③ 阈值判定：LLM full → seeker（FAISS 只做极端否决）；
#      LLM partial/none → advisor
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from datetime import datetime

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, log_node_event
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

# 修改触发词：用户说这些词表示在修改方案，应强制路由到 Advisor 重新确认
MODIFICATION_KEYWORDS = ["不对", "不是", "换成", "修改", "调整", "改", "换", "错了"]


def _is_modification(user_response: str) -> bool:
    """判断用户输入是否为修改意图（简单关键词匹配）"""
    if not user_response:
        return False
    text = user_response.strip()
    for keyword in MODIFICATION_KEYWORDS:
        if keyword in text:
            return True
    return False


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

        # ── 读取独立的 confirmed_plan（Advisor 写入，Planner 只读不改）──
        confirmed_plan = state.get("confirmed_plan") or {}
        has_confirmed = bool(confirmed_plan.get("tables"))
        confirmed_context = ""
        if has_confirmed:
            tables = confirmed_plan.get("tables", [])
            fields = confirmed_plan.get("fields", [])
            confirmed_context = (
                "【上一轮已确认的分析方案】\n"
                f"表: {', '.join(tables)}\n"
                f"字段: {', '.join(fields)}\n"
                "请根据用户本轮实际输入判断：用户是确认方案（→ full）、修改方案（→ partial）、还是换话题（→ none）"
            )

        # ── 获取 Advisor 上轮回复，帮助 LLM 理解用户的简短选择 ──
        advisor_last_answer = state.get("advisor_last_answer", "")
        if advisor_last_answer:
            advisor_last_answer = (
                "【上一轮 Advisor 的回复】\n"
                f"{advisor_last_answer[:800]}\n"
                "如果用户输入是数字或简短选择，请从此回复中推断对应的是哪个选项。"
            )
        else:
            advisor_last_answer = ""

        # ── FAISS + LLM 评估流程 ──
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

            # ── 新增：检索历史优质示例，辅助模糊度判定 ──
            example_vs = runtime.get("example_vector_store")
            example_context = ""
            if example_vs:
                examples = example_vs.search_similar(question, k=2)
                if examples:
                    lines = []
                    for doc in examples:
                        q = doc.metadata.get("question", "")
                        lines.append(f"- {q}")
                    example_context = "\n".join(lines)


            # ── 步骤②：LLM 结构化解析（含 user_response、confirmed_context、advisor_last_answer）──
            user_response = state.get("user_response", question)
            prompt_value = prompt.invoke({
                "question": question,
                "user_response": user_response,
                "metadata_context": metadata_context,
                "example_context": example_context,
                "confirmed_context": confirmed_context,
                "advisor_last_answer": advisor_last_answer,
            })
            planner_output = structured_llm.invoke(prompt_value)

            # ── 信任 LLM 的 completeness 判定，不做覆盖 ──
            tables = planner_output.tables
            fields = planner_output.fields
            completeness = planner_output.completeness

            # 后校验：修改场景强制路由 ──
            is_modifying = has_confirmed and _is_modification(user_response)
            if is_modifying:
                completeness = "partial"
                if not planner_output.reason:
                    planner_output.reason = "用户在已确认方案的基础上提出修改，需 Advisor 重新确认"

            # 兜底：LLM 未填 completeness 或填了无效值
            if completeness not in ("full", "partial", "none"):
                if not tables:
                    completeness = "none"
                elif not fields:
                    completeness = "partial"
                else:
                    completeness = "full"

            # ── 用户确认时，复用 confirmed_plan 的精确字段 ──
            if completeness == "full" and has_confirmed:
                tables = confirmed_plan.get("tables", tables)
                fields = confirmed_plan.get("fields", fields)

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

            # ── 步骤③：阈值判定 ──
            high_similarity_count = 0
            for doc, score in column_docs_with_scores:
                similarity = 1 - score / 2
                if similarity > HIGH_SIMILARITY_THRESHOLD:
                    high_similarity_count += 1

            if completeness in ("none", "partial"):
                route = "advisor"
                planner_reason = f"LLM判定{completeness}: " + planner_output.reason
                if is_modifying:
                    planner_reason = f"LLM判定{completeness}(修改场景强制): " + planner_output.reason
            elif high_similarity_count > MAX_HIGH_SIMILARITY_COUNT * 3:
                route = "advisor"
                planner_reason = f"LLM判定full但字段候选过多({high_similarity_count} > {MAX_HIGH_SIMILARITY_COUNT * 3}): {planner_output.reason}"
            else:
                route = "seeker"
                planner_reason = "LLM判定映射完整: " + planner_output.reason

            # ── planner_entities 只写 Planner 自己的分析结果，不写 confirmed 标记 ──
            # confirmed 由 confirmed_plan 独立管理
            new_entities = {
                "table": tables[0] if tables else "",
                "tables": tables,
                "fields": fields,
                "measures": [],       # Planner不区分度量/维度，留空
                "dimensions": [],
                "time_field": "pt_dt",
                "filters": "",
                "completeness": completeness,
            }

            log_node_end(
                "planner",
                route=route,
                completeness=completeness,
                tables=str(tables),
                fields=str(fields),
                high_sim=high_similarity_count,
                reason=planner_reason,
                ms=elapsed_ms(timer),
            )
            log_sub_info(f"表: {table_scores_str}")
            log_sub_info(f"字段: {column_scores_str}")

            return_value = {
                "route": route,
                "planner_reason": planner_reason,
                "original_question": original_question,
                "planner_entities": new_entities,
            }


            # Planner 路由 seeker 时，若 Advisor 未写入 confirmed_plan，自动提升 planner_entities 为兜底方案
            # 确保 generate_sql 的一致性校验能检测到方案偏差（如误用不在方案中的字段）
            if route == "seeker" and not has_confirmed:
                # 企业级规则：没有confirmed_plan就不能进Seeker，强制路由Advisor
                route = "advisor"
                planner_reason = "无已确认方案（Advisor未调用confirm_selection），需Advisor澄清后重新判定"
                log_node_event("planner", "强制路由advisor: 缺少confirmed_plan")
                return_value["route"] = route
                return_value["planner_reason"] = planner_reason

            return return_value

        except Exception as error:
            log_node_error("planner", error=str(error), ms=elapsed_ms(timer))
            raise

    return planner_node
