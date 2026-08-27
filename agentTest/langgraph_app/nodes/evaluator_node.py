# Evaluator 评估节点：收集指标 → LLM 自评 → 计算综合分 → 入库
# 放在 Seeker 子图 build_final_answer 之后，只在 Seeker 通道触发
# 始终入库 MySQL（返回 dialogue_id 供用户后续打分），FAISS 仅高分入库
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body
from agentTest.config.evaluator import (
    WEIGHT_TIME, WEIGHT_TURNS, WEIGHT_LLM_SELF, WEIGHT_USER,
    HIGH_QUALITY_THRESHOLD, DEFAULT_USER_SCORE,
    BASE_TURNS, TURNS_PER_METRIC, TURNS_PER_DIMENSION,
    TURNS_PER_EXTRA_TABLE, COMPLEX_TURN_BONUS,
    TIME_BASE_MS, TIME_PER_METRIC_MS, TIME_PER_DIMENSION_MS,
    TIME_PER_TABLE_MS, TIME_PER_FIELD_MS, COMPLEX_TIME_MS,
    score_by_budget,
)
from agentTest.langgraph_app.prompts.evaluator_prompt import EvaluatorSelfScore, EVALUATOR_SYSTEM_PROMPT, EVALUATOR_USER_TEMPLATE
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.message_utils import build_advisor_dialogue_context
from agentTest.langgraph_app.runtime.graph_logger import (
    log_node_start, log_node_end, log_node_degraded, elapsed_ms, start_timer,
)
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.langgraph_app.runtime.llm_log_handler import build_llm_logging_handler
from agentTest.langchain_app.vectorstores.example_vector_store import example_hash_id
from agentTest.metadata.mysql_store import save_evaluated_dialogue, load_enriched_tables


def _estimate_complexity(state) -> tuple[float, float]:
    """根据查询复杂度估算期望澄清轮次与期望耗时预算。

    复杂度信号来自 AnalysisSpec（指标/维度概念）与最终确认方案（表/字段/复杂标志），
    指标数/维度数取两者较大值防止概念被清理时低估；计数封顶避免极端输入撑爆预算。
    """
    spec = state.get("analysis_spec") or {}
    plan = state.get("confirmed_plan") or {}

    metric_count = max(
        len(spec.get("metric_mentions") or []),
        len(plan.get("measures") or []),
    )
    dimension_count = max(
        len(spec.get("dimension_mentions") or []),
        len(plan.get("dimensions") or []),
    )
    table_count = len(plan.get("tables") or [])
    field_count = len(plan.get("fields") or [])
    is_complex = bool(plan.get("complex")) or bool(spec.get("comparison"))

    # 封顶防止极端输入
    metric_count = min(metric_count, 10)
    dimension_count = min(dimension_count, 5)
    table_count = min(table_count, 5)
    field_count = min(field_count, 20)

    expected_turns = (
        BASE_TURNS
        + TURNS_PER_METRIC * metric_count
        + TURNS_PER_DIMENSION * dimension_count
        + TURNS_PER_EXTRA_TABLE * max(table_count - 1, 0)
        + (COMPLEX_TURN_BONUS if is_complex else 0)
    )
    expected_time_ms = (
        TIME_BASE_MS
        + TIME_PER_METRIC_MS * metric_count
        + TIME_PER_DIMENSION_MS * dimension_count
        + TIME_PER_TABLE_MS * table_count
        + TIME_PER_FIELD_MS * field_count
        + (COMPLEX_TIME_MS if is_complex else 0)
    )
    return expected_turns, expected_time_ms


def build_evaluator_node(runtime):
    """构建 Evaluator 节点：LLM 自评打分 + 入库（MySQL 始终 / FAISS 仅高分）"""
    example_vector_store = runtime.get("example_vector_store")
    llm = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,
        extra_body=get_model_extra_body(),
        callbacks=[build_llm_logging_handler("evaluator")],
    )
    structured_llm = llm.with_structured_output(EvaluatorSelfScore)

    prompt = ChatPromptTemplate.from_messages([
        ("system", EVALUATOR_SYSTEM_PROMPT),
        ("human", EVALUATOR_USER_TEMPLATE),
    ])

    def evaluator_node(state: AgentState):
        question = state.get("effective_query", "") or ""
        effective_query = question
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
            # 按查询复杂度估算期望预算：指标/维度多、多表、复杂查询的轮次与耗时预算相应放大
            expected_turns, expected_time_ms = _estimate_complexity(state)
            turn_score = score_by_budget(max(advisor_turns, 0), expected_turns)
            time_score = score_by_budget(total_time_ms, expected_time_ms) if total_time_ms else 50

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
                expected_turns=round(expected_turns, 1),
                expected_time_ms=round(expected_time_ms, 1),
                ms=elapsed_ms(timer),
            )

            # 节点完成后记录 State 分层摘要，供 trace 查看数据流转
            log_state_snapshot("evaluator", {**state, **{
                "evaluator_score": comprehensive,
                "evaluator_self_score": round(llm_self_avg, 1),
                "evaluator_dialogue_id": dialogue_id or 0,
            }})

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
