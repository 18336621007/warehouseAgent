# Evaluator 评估节点：收集指标 → LLM 自评 → 计算综合分 → 入库
# 放在 Seeker 子图 build_final_answer 之后，只在 Seeker 通道触发
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.config.evaluator import (
    WEIGHT_TIME, WEIGHT_TURNS, WEIGHT_LLM_SELF, WEIGHT_USER,
    HIGH_QUALITY_THRESHOLD, DEFAULT_USER_SCORE,
    TURN_SCORE_MAP, TIME_SCORE_MAP, map_value_to_score,
)
from agentTest.langgraph_app.prompts.evaluator_prompt import EvaluatorSelfScore, EVALUATOR_SYSTEM_PROMPT, EVALUATOR_USER_TEMPLATE
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.runtime.graph_logger import (
    log_node_start, log_node_end, log_node_error, elapsed_ms, start_timer,
)
from agentTest.metadata.mysql_store import save_evaluated_dialogue, load_enriched_tables


def build_evaluator_node(runtime):
    """构建 Evaluator 节点：LLM 自评打分 + 高分对话入库（MySQL + FAISS）"""
    example_vector_store = runtime.get("example_vector_store")
    llm = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,
    )
    structured_llm = llm.with_structured_output(EvaluatorSelfScore)

    prompt = ChatPromptTemplate.from_messages([
        ("system", EVALUATOR_SYSTEM_PROMPT),
        ("human", EVALUATOR_USER_TEMPLATE),
    ])

    def evaluator_node(state: AgentState):
        question = state.get("question", "")
        route = state.get("route", "seeker")
        planner_reason = state.get("planner_reason", "")
        advisor_turns = state.get("advisor_turns", 0)
        generated_sql = state.get("generated_sql", "")
        final_answer = state.get("final_answer", "")
        advisor_messages = state.get("advisor_messages") or []

        timer = start_timer()
        log_node_start("evaluator", question=question[:40], turns=advisor_turns)

        try:
            # ── 步骤1：计算客观指标 ──
            total_time_ms = state.get("total_topic_time_ms", 0)
            time_score = map_value_to_score(total_time_ms, TIME_SCORE_MAP) if total_time_ms else 50
            turn_score = map_value_to_score(max(advisor_turns, 0), TURN_SCORE_MAP)

            # ── 步骤2：LLM 自评 ──
            advisor_context = "无澄清过程，直接进入 Seeker"
            if advisor_messages:
                user_visible = []
                for msg in advisor_messages:
                    content = getattr(msg, "content", "") if hasattr(msg, "content") else str(msg)
                    if content and not getattr(msg, "tool_calls", None):
                        user_visible.append(str(content)[:300])
                if user_visible:
                    advisor_context = " | ".join(user_visible[-5:])

            prompt_value = prompt.invoke({
                "question": question,
                "route": route,
                "planner_reason": planner_reason,
                "advisor_turns": advisor_turns,
                "advisor_context": advisor_context[:2000],
                "sql": generated_sql[:2000],
                "final_answer": str(final_answer)[:2000],
            })
            self_score: EvaluatorSelfScore = structured_llm.invoke(prompt_value)

            # ── 步骤3：计算综合分 ──
            llm_self_avg = (self_score.coherence_score + self_score.satisfaction_score) / 2
            comprehensive = round(
                WEIGHT_TIME * time_score
                + WEIGHT_TURNS * turn_score
                + WEIGHT_LLM_SELF * llm_self_avg
                + WEIGHT_USER * DEFAULT_USER_SCORE,
                1,
            )
            is_high_quality = comprehensive >= HIGH_QUALITY_THRESHOLD

            # ── 步骤4：高分对话入库（MySQL + FAISS 双写）──
            if is_high_quality:
                confirmed_plan = state.get("confirmed_plan") or {}
                tables_used = confirmed_plan.get("tables", [])
                fields_used = confirmed_plan.get("fields", [])

                # 构建解析后问题：原始问题 + 确认方案摘要
                # 防止模糊问题被误匹配（如"回流订单数"被错误关联到"月租订单"口径）
                resolved_question = str(question)
                if confirmed_plan.get("tables"):
                    tables_str = ", ".join(tables_used)
                    fields_str = ", ".join(fields_used)
                    resolved_question = (
                        f"{question}"
                        f"（确认方案: 表={tables_str}, 字段={fields_str}）"
                    )

                # 从增强元数据获取 domain_tag
                enriched = load_enriched_tables()
                domain_tag = ""
                for tbl in tables_used:
                    if tbl in enriched:
                        domain_tag = enriched[tbl].get("domain", "")
                        break

                # MySQL 写入
                try:
                    save_evaluated_dialogue(
                        question=str(question),
                        resolved_question=resolved_question,
                        sql=str(generated_sql),
                        answer=str(final_answer),
                        tables_used=tables_used,
                        fields_used=fields_used,
                        advisor_turns=advisor_turns,
                        total_time_ms=total_time_ms,
                        time_score=time_score,
                        turn_score=turn_score,
                        llm_self_score=llm_self_avg,
                        comprehensive_score=comprehensive,
                        domain_tag=domain_tag,
                    )
                except Exception as db_error:
                    log_node_error("evaluator", error=f"MySQL入库失败: {db_error}")

                # FAISS 写入：page_content 用 resolved_question，语义检索更精准
                if example_vector_store is not None:
                    try:
                        example_vector_store.add_example(
                            question=resolved_question,  # 用解析后问题，而非原始模糊问题
                            sql=str(generated_sql),
                            answer=str(final_answer),
                            tables=tables_used,
                            fields=fields_used,
                            domain_tag=domain_tag,
                            score=comprehensive,
                        )
                    except Exception as faiss_error:
                        log_node_error("evaluator", error=f"FAISS入库失败: {faiss_error}")

            log_node_end(
                "evaluator",
                comprehensive=comprehensive,
                time=round(time_score, 1),
                turns=round(turn_score, 1),
                self=round(llm_self_avg, 1),
                high_quality=is_high_quality,
                comment=self_score.brief_comment,
                ms=elapsed_ms(timer),
            )

            return {
                "evaluator_score": comprehensive,
                "evaluator_self_score": round(llm_self_avg, 1),
            }

        except Exception as error:
            log_node_error("evaluator", error=str(error), ms=elapsed_ms(timer))
            raise

    return evaluator_node
