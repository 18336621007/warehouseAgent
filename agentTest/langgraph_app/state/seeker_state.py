# ── state/seeker.py ──
# Seeker 子图字段
from typing import List, Any
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
    result_csv: str

    final_answer: str
    retry_count: int
    sql_fix_reason: str
    # Seeker 只接受 status=locked 的完整查询方案
    confirmed_plan: QueryPlan         # 当前查询方案（draft/locked），只读，SQL 一致性校验用

    # SQL 执行重试相关字段
    sql_exec_failed: bool         # SQL 执行是否失败
    sql_exec_error: str           # Hive 返回的错误信息
    exec_retry_count: int         # SQL 执行重试次数

    # 一致性校验通过的 SQL 历史，exec_retry 时回灌给 LLM 参考，避免反复瞎试
    sql_pass_history: List[str]
