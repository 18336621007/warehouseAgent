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


# 负责身份信息：它们会自动被 Planner、Advisor、Seeker 继承。
class IdentityState(TypedDict, total=False):

    conversation_id: str  #前端左端的一个对话
    topic_id: str   # 该对话中的一次问数任务
    request_id: str # 一次HTTP请求

# 负责一次问数任务的记忆：
class TopicState(IdentityState, total=False):
    # messages 只保存当前 Topic 的消息，并通过 Reducer 增量合并
    messages: Annotated[list[AnyMessage], add_messages] # 节点以后只需要返回新增消息,会自动追加到消息列表

    # 话题原始问题，在同一个 Topic 中保持不变。新话题创建时更新原始问题
    original_question: str

    # Planner 每轮改写后的有效需求，original_question 保留原文、本字段滚动更新
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
