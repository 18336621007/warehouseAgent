# Evaluator 评估节点：收集指标 → LLM 自评 → 计算综合分 → 入库
# 放在 Seeker 子图 build_final_answer 之后，只在 Seeker 通道触发
# 始终入库 MySQL（返回 dialogue_id 供用户后续打分），FAISS 仅高分入库
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body
from agentTest.config.evaluator import (
    WEIGHT_TIME, WEIGHT_TURNS, WEIGHT_LLM_SELF, WEIGHT_USER,
    HIGH_QUALITY_THRESHOLD, DEFAULT_USER_SCORE,
    TURN_SCORE_MAP, TIME_SCORE_MAP, map_value_to_score,
)
from agentTest.langgraph_app.prompts.evaluator_prompt import EvaluatorSelfScore, EVALUATOR_SYSTEM_PROMPT, EVALUATOR_USER_TEMPLATE
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.message_utils import build_advisor_dialogue_context
from agentTest.langgraph_app.runtime.graph_logger import (
    log_node_start, log_node_end, log_node_degraded, elapsed_ms, start_timer,
)
from agentTest.langchain_app.vectorstores.example_vector_store import example_hash_id
from agentTest.metadata.mysql_store import save_evaluated_dialogue, load_enriched_tables


def build_evaluator_node(runtime):
    """构建 Evaluator 节点：LLM 自评打分 + 入库（MySQL 始终 / FAISS 仅高分）"""
    example_vector_store = runtime.get("example_vector_store")
    llm = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,
        extra_body=get_model_extra_body(),
    )
    structured_llm = llm.with_structured_output(EvaluatorSelfScore)

    prompt = ChatPromptTemplate.from_messages([
        ("system", EVALUATOR_SYSTEM_PROMPT),
        ("human", EVALUATOR_USER_TEMPLATE),
    ])

    def evaluator_node(state: AgentState):
        question = state.get("original_question", "")
        effective_query = state.get("effective_query", "") or question
        route = state.get("route", "seeker")
        planner_reason = state.get("planner_reason", "")
        advisor_turns = state.get("advisor_turns", 0)
        generated_sql = state.get("generated_sql", "")
        final_answer = state.get("final_answer", "")
        messages = state.get("messages") or []

        timer = start_timer()
        log_node_start("evaluator", question=question[:40], turns=advisor_turns)

        try:
            # ── 步骤1：计算客观指标 ──
            total_time_ms = state.get("total_topic_time_ms", 0)
            time_score = map_value_to_score(total_time_ms, TIME_SCORE_MAP) if total_time_ms else 50
            turn_score = map_value_to_score(max(advisor_turns, 0), TURN_SCORE_MAP)

            # ── 步骤2：LLM 自评 ──
            # 只提取用户和Advisor之间的可见澄清对话
            advisor_context = build_advisor_dialogue_context(messages)
            if not advisor_context:
                advisor_context = "无澄清过程，直接进入 Seeker"

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

            # ── 步骤3：计算综合分（初始用户分 = 默认 75）──
            llm_self_avg = (self_score.coherence_score + self_score.satisfaction_score) / 2
            user_score = DEFAULT_USER_SCORE
            comprehensive = round(
                WEIGHT_TIME * time_score
                + WEIGHT_TURNS * turn_score
                + WEIGHT_LLM_SELF * llm_self_avg
                + WEIGHT_USER * user_score,
                1,
            )
            is_high_quality = comprehensive >= HIGH_QUALITY_THRESHOLD

            # ── 步骤4：构建 resolved_question 和 domain_tag（入库共用）──
            confirmed_plan = state.get("confirmed_plan") or {}
            tables_used = confirmed_plan.get("tables", [])
            fields_used = confirmed_plan.get("fields", [])

            resolved_question = str(effective_query)
            if confirmed_plan.get("tables"):
                tables_str = ", ".join(tables_used)
                fields_str = ", ".join(fields_used)
                resolved_question = f"{effective_query}（确认方案: 表={tables_str}, 字段={fields_str}）"

            enriched = load_enriched_tables()
            domain_tag = ""
            for tbl in tables_used:
                if tbl in enriched:
                    domain_tag = enriched[tbl].get("domain", "")
                    break

            # ── 步骤5：MySQL 始终入库，返回 ID 供用户后续打分 ──
            dialogue_id = None
            try:
                example_hash = example_hash_id(question)
                dialogue_id = save_evaluated_dialogue(
                    question=str(question),
                    effective_query=effective_query,
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
                    user_score=user_score,
                example_hash=example_hash,
                )
            except Exception as db_error:
                log_node_degraded(
                    "evaluator",
                    db_error,
                    error_code="EVALUATOR_MYSQL_DEGRADED",
                    stage="mysql_persist",
                )

            # ── 步骤6：FAISS 仅高分入库 ──
            if is_high_quality and example_vector_store is not None:
                try:
                    # hash_id already computed above, reuse
                    example_vector_store.add_example(
                        question=question,
                        effective_query=effective_query,
                        sql=str(generated_sql),
                        answer=str(final_answer),
                        tables=tables_used,
                        fields=fields_used,
                        domain_tag=domain_tag,
                        score=comprehensive,
                    )
                except Exception as faiss_error:
                    log_node_degraded(
                        "evaluator",
                        faiss_error,
                        error_code="EVALUATOR_FAISS_DEGRADED",
                        stage="faiss_persist",
                    )

            log_node_end(
                "evaluator",
                comprehensive=comprehensive,
                user=round(user_score, 1),
                time=round(time_score, 1),
                turns=round(turn_score, 1),
                self=round(llm_self_avg, 1),
                high_quality=is_high_quality,
                dialogue_id=dialogue_id,
                comment=self_score.brief_comment,
                ms=elapsed_ms(timer),
            )

            return {
                "evaluator_score": comprehensive,
                "evaluator_self_score": round(llm_self_avg, 1),
                "evaluator_dialogue_id": dialogue_id or 0,
            }

        except Exception as error:
            log_node_degraded(
                "evaluator",
                error,
                error_code="EVALUATOR_DEGRADED",
                stage="evaluation",
                ms=elapsed_ms(timer),
            )

            # Evaluator属于查询完成后的附加能力，失败不能影响最终答案
            return {
                "evaluator_score": 0,
                "evaluator_self_score": 0,
                "evaluator_dialogue_id": 0,
            }

    return evaluator_node
