# ── state/analysis_spec.py ──
# AnalysisSpec：从自然语言提取的结构化业务分析意图，跨轮保留在 Topic State 中。
# ConceptResolution：单个业务概念（指标/维度）的候选与解析证据，未解决候选不进入 QueryPlan。
from typing import TypedDict


class ConceptCandidate(TypedDict, total=False):
    """一个业务概念对应的物理字段候选，必须来自真实元数据。"""
    table: str              # database.table 完整表标识
    field: str              # 物理字段名
    semantic_type: str      # measure / dimension
    comment: str            # 中文业务说明（来自元数据注释或检索文本）
    aliases: list[str]      # 业务别名
    score: float            # 检索排序分数，仅用于排序，不产生解析证据


class ConceptResolution(TypedDict, total=False):
    """业务概念的解析记录：status=ambiguous 表示仍需用户选择口径。"""
    mention: str            # 用户提到的业务概念，如"新增订单"
    concept_type: str       # metric / dimension
    status: str             # ambiguous / resolved   （未定义/用户已确认）
    selected_field: str     # 已选物理字段，未解析时为空
    selected_table: str     # 已选字段所属表
    resolution_source: str  # llm_submitted / explicit_user / unique_metadata / semantic_default / unknown
    candidates: list[dict]  # ConceptCandidate 列表（按排序分数降序）

# 跨轮保存“用户分析意图 + 指标解析证据”的结构化状态
class AnalysisSpec(TypedDict, total=False):
    """从自然语言提取的结构化分析意图，作为指标歧义门禁和后续语义层的公共入口。"""
    analysis_type: str                # detail / aggregate / trend / ranking / comparison
    metric_mentions: list[str]        # 用户提到的指标业务概念
    dimension_mentions: list[str]     # 用户提到的维度业务概念
    time_range: str                   # 时间范围，如"昨天"
    time_grain: str                   # 时间粒度 day/week/month/quarter/year
    filters: list[dict]               # 用户业务过滤条件
    order_by: list[dict]              # 排序规则 [{"concept": "...", "direction": "DESC"}]
    limit: int                        # TopN 数量
    comparison: dict                  # 同比/环比/基期对比
    metric_resolutions: list[ConceptResolution]  # 指标解析证据，跨轮保留候选
    pending_metric_clarification: dict  # 当前待确认指标及固定顺序候选，供下一轮解析编号(上轮给用户看的候选列表)
