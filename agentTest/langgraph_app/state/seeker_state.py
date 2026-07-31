# ── state/seeker.py ──
# Seeker 子图字段 + Evaluator 字段（同属一条执行链路）
from typing import List, Any, TypedDict
from agentTest.langgraph_app.state.base_state import BaseState
from agentTest.langgraph_app.state.query_plan import QueryPlan

class SeekerState(BaseState, total=False):
    # Seeker SQL 生成链路
    schema_documents: List[Any]
    schema_context: str

    # 新状态只保存候选标识，避免长期持久化完整 Document
    schema_candidate_ids: List[str]

    generated_sql: str
    sql_valid: bool
    sql_error: str
    sql_result: Any

    # 完整结果后续交由独立存储管理，State只保存引用和预览
    result_id: str
    result_preview: List[Any]

    final_answer: str
    retry_count: int
    sql_fix_reason: str
    # Seeker 只接受 status=confirmed 的完整查询方案
    confirmed_plan: QueryPlan         # 只读，SQL 一致性校验用


    # Evaluator 评估字段，advisor_turns从TopicState继承并由Graph自动累积
    total_topic_time_ms: float   # 本次话题总耗时（demo 层传入）
    evaluator_score: float       # 综合评分（Evaluator 写入）

    # 补充被旧注释覆盖的Evaluator字段
    evaluator_self_score: float  # LLM 自评分数（Evaluator 写入）
    evaluator_dialogue_id: int   # MySQL evaluated_dialogues 主键，供用户打分更新
