# ── state/base_state.py ──
# 所有子图共享的基础字段
from typing import Annotated, Literal, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from agentTest.langgraph_app.state.query_plan import QueryPlan
from agentTest.langgraph_app.state.analysis_spec import AnalysisSpec

# Topic 状态用于描述一次问数任务的生命周期
TopicStatus = Literal[
    "new",
    "clarifying",
    "confirmed",
    "generating_sql",
    "validating_sql",
    "executing",
    "completed",
    "failed",
    "cancelled",
]

# 上一轮查询结果快照：只保存引用+预览+实体键，不把全量结果写入 checkpoint
class QueryResultSnapshot(TypedDict, total=False):
    result_id: str            # 结果快照 ID，格式 {request_id}:result
    source_request_id: str    # 产生该结果的请求 ID
    confirmed_plan: dict      # 当前查询方案（draft/locked/confirmed），不复制全量
    columns: list[str]        # 结果列名
    preview_rows: list[dict]  # 预览行（数量上限见 build_final_answer_node）
    row_count: int            # 总行数
    result_summary: str       # 一句话摘要
    entity_keys: list[str]    # 实体键（首个维度字段值），结果追问用
    created_at: str           # ISO 时间
    result_file: str          # 结果 JSON 文件路径（result_store 落盘引用）
    full_csv: str             # 全量 CSV 文件路径（交付/导出用）
    round_no: int             # 会话内结果轮次（跨轮引用用）


# 负责身份信息：它们会自动被 Planner、Advisor、Seeker 继承。
class IdentityState(TypedDict, total=False):

    conversation_id: str  #前端左端的一个对话
    topic_id: str   # 该对话中的一次问数任务
    request_id: str # 一次HTTP请求

# 负责一次问数任务的记忆：
class TopicState(IdentityState, total=False):
    # messages 只保存当前 Topic 的消息，并通过 Reducer 增量合并
    messages: Annotated[list[AnyMessage], add_messages] # 节点以后只需要返回新增消息,会自动追加到消息列表

    # 去 Topic 化：不再保存 original_question 固定基线；
    # Planner 每轮基于完整历史 + 本轮输入改写 effective_query（query 改写）作为当前需求
    effective_query: str

    # 当前输入，每轮更新
    current_user_input: str

    topic_status: TopicStatus
    topic_summary: str
    topic_started_at: float

    advisor_turns: int

    # 当前查询方案，后续会统一重命名为 query_plan
    confirmed_plan: QueryPlan

    # 结构化业务分析意图，跨轮保留指标候选与解析证据，供指标歧义门禁使用
    analysis_spec: AnalysisSpec

    # 上一轮查询结果快照（引用+预览+实体键），结果追问用
    last_query_result: QueryResultSnapshot



class BaseState(TopicState, total=False):
    # 公共流程字段
    current_node: str
    error_message: str

    # Seeker 方案不可行时的失败原因（缺 join 契约等），触发回 Planner 修复
    seeker_plan_error: str
    # 不可修复错误标志（如缺 join 契约）：置位时跳过 Planner repair，直接告知用户
    seeker_error_unresolvable: bool
    # 已消费的执行失败修复轮次，用于限制回 Planner 次数
    plan_repair_rounds: int

    # Advisor 本轮是否推进了草稿（兼容旧字段，供 trace 参考；路由已改用 advisor_next_step）
    advisor_draft_updated: bool
    # Advisor 连续自动回 Planner 的轮次（防 planner↔advisor 死循环）
    advisor_auto_rounds: int
    # Seeker 执行成功但 0 行时的自愈标记与轮次（回 Planner 用 probe_values 确认实际取值）
    seeker_empty_result: bool
    empty_result_rounds: int
    # 0 行自愈旁白：自然语言说明"发现空结果 → 返回修正"，供前端思考过程展示
    self_heal_note: str
    # Advisor 收尾结构化动作：wait_user=等用户，return_to_planner=回 Planner 再判定
    advisor_next_step: str
