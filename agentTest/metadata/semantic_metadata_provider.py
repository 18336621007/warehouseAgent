# 语义元数据 Provider，统一管理表定义和 Join 关系
# 数据来源：哈尔滨语义层 YAML（SemanticLayerProvider，唯一权威来源）
# 已废弃 semantic_metadata.json（表/关系/粒度均由语义层 YAML 提供）
from typing import Optional

from agentTest.semantic_layer.semantic_layer_provider import (
    SemanticLayerProvider,
    get_semantic_layer_provider,
)


class SemanticMetadataProvider:
    """语义元数据查询服务，为 JoinPlanner/TableCoverageAnalyzer/Planner 提供统一数据。

    数据全部来自语义层 YAML（SemanticLayerProvider）：
    - 指标（metrics）、实体（entities）、物理表（physical）、逻辑模型（semantic_models）
    - Join 契约（join_contracts）、分区（partition）、候选键（candidate_keys）
    """

    def __init__(self, semantic_layer: Optional[SemanticLayerProvider] = None):
        self._semantic_layer = semantic_layer or get_semantic_layer_provider()

    # ── JoinPlanner 用 ──

    def get_relations_for_table(self, table_identifier: str) -> list[dict]:
        """获取指定表参与的所有合规 Join 契约（语义层 join_contracts）。"""
        return self._semantic_layer.get_join_contracts_for_model(table_identifier)

    def get_relation_between(
        self, left_table: str, right_table: str
    ) -> list[dict]:
        """获取两张表之间的直接 Join 契约（忽略方向）。"""
        contracts = self._semantic_layer.get_all_join_contracts()
        result = []
        for rel in contracts:
            if (rel.get("left_model") == left_table and rel.get("right_model") == right_table) or \
               (rel.get("left_model") == right_table and rel.get("right_model") == left_table):
                result.append(rel)
        return result

    def get_table_grain(self, table_identifier: str) -> str:
        """获取指定表的数据粒度（语义层 semantic_models grain.keys，逗号拼接）。"""
        model = self._semantic_layer.get_semantic_model(table_identifier)
        if model:
            keys = (model.get("grain") or {}).get("keys") or []
            if keys:
                return ", ".join(str(k) for k in keys)
        return ""

    def get_table_primary_key(self, table_identifier: str) -> list[str]:
        """获取指定表的主键候选（语义层 physical grain.candidate_keys）。"""
        info = self._semantic_layer.get_physical_table(table_identifier)
        if info:
            return list(info.get("candidate_keys") or [])
        return []

    def get_table_time_field(self, table_identifier: str) -> Optional[str]:
        """获取指定表的时间字段（语义层 physical partition 中第一个非平台字段）。"""
        partitions = self._semantic_layer.get_partition_fields(table_identifier)
        for f in partitions:
            if f not in ("pt_platform",):
                return f
        return partitions[0] if partitions else None

    # ── 枚举接口 ──

    def get_all_tables(self) -> list[dict]:
        """获取所有物理表（语义层 physical YAML）。"""
        return self._semantic_layer.get_all_physical_tables()

    def get_all_enabled_relations(self) -> list[dict]:
        """获取所有合规 Join 契约（语义层 join_contracts）。"""
        return self._semantic_layer.get_all_join_contracts()

    def reload(self):
        """重新加载语义层配置，用于热更新。"""
        self._semantic_layer.reload()

    # ── 语义层直通接口 ──

    @property
    def semantic_layer(self) -> SemanticLayerProvider:
        return self._semantic_layer

    def get_metric_suggestions(self, query: str, limit: int = 5) -> list[dict]:
        """从用户问题匹配业务指标，返回 [{id, name, source_model, expression, ...}, ...]"""
        return self._semantic_layer.match_metrics_from_query(query)[:limit]

    def get_metric_by_id(self, metric_id: str) -> Optional[dict]:
        return self._semantic_layer.get_metric_by_id(metric_id)

    def get_entity_suggestions(self, keyword: str) -> Optional[dict]:
        """按别名匹配实体（如"电池"/"经销商"），返回 {key, name, key_field, aliases}"""
        return self._semantic_layer.get_entity_by_keyword(keyword)

    def get_all_entities(self) -> list[dict]:
        return self._semantic_layer.get_all_entities()

    def get_physical_table(self, full_name: str) -> Optional[dict]:
        """按 schema.table 获取物理表信息（含 partition / fields / candidate_keys）"""
        return self._semantic_layer.get_physical_table(full_name)

    def get_partition_fields(self, full_name: str) -> list[str]:
        """获取指定物理表的分区字段（如 pt_dt, pt_platform）。"""
        return self._semantic_layer.get_partition_fields(full_name)

    def get_table_fields(self, full_name: str) -> list[str]:
        """获取指定物理表的所有字段名列表"""
        return self._semantic_layer.get_table_fields(full_name)

    def is_field_in_table(self, full_name: str, field_name: str) -> bool:
        """精确校验字段是否属于指定物理表（基于语义层 YAML 权威定义）"""
        return self._semantic_layer.is_field_in_table(full_name, field_name)

    def get_join_contract(self, left_model: str, right_model: str) -> Optional[dict]:
        """获取 join_contracts.yaml 中两表之间的合规 Join 路径"""
        return self._semantic_layer.get_join_contract(left_model, right_model)

    def get_all_join_contracts(self) -> list[dict]:
        return self._semantic_layer.get_all_join_contracts()

    def find_safe_join_path(self, models: list[str]) -> list[dict]:
        """在给定模型集合中寻找合规 Join 边列表（BFS 生成最小生成树）"""
        return self._semantic_layer.find_safe_join_path(models)
