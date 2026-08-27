# ── state/analysis_spec.py ──
# AnalysisSpec：从自然语言提取的结构化业务分析意图，跨轮保留在 Conversation State 中。
# 去 pending 状态机：不再跨轮保存解析证据/候选快照，澄清靠历史 + effective_query 改写。
from typing import TypedDict


class ConceptCandidate(TypedDict, total=False):
    """一个业务概念对应的物理字段候选，必须来自真实元数据。"""
    table: str              # database.table 完整表标识
    field: str              # 物理字段名
    semantic_type: str      # measure / dimension
    comment: str            # 中文业务说明（来自元数据注释或检索文本）
    aliases: list[str]      # 业务别名
    score: float            # 检索排序分数，仅用于排序，不产生解析证据


# 跨轮保存“用户分析意图”的结构化状态（仅当轮字段；解析证据/候选快照已去 pending 化）
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
