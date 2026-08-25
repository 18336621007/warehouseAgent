# 语义元数据 Provider，统一管理表定义和 Join 关系
# 数据来源：
#   - 人工配置的 semantic_metadata.json（兼容老接口）
#   - 哈尔滨语义层 YAML（推荐来源，结构更完整）
# 后续语义层扩展时，优先调用 SemanticLayerProvider，新增方法在末尾追加以保证兼容性
import json
import os
from typing import Optional

from agentTest.semantic_layer.semantic_layer_provider import (
    SemanticLayerProvider,
    get_semantic_layer_provider,
)


class SemanticMetadataProvider:
    """语义元数据查询服务，为 JoinPlanner/TableCoverageAnalyzer/Planner 提供统一数据。

    该类同时暴露两类数据：
    1) 老接口（基于 semantic_metadata.json）：用于向后兼容 JoinPlanner
    2) 新接口（基于 SemanticLayerProvider）：用于扩展能力（指标/实体/Join契约/物理表）
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        semantic_layer: Optional[SemanticLayerProvider] = None,
    ):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__),
                "semantic_metadata.json",
            )
        self.config_path = config_path
        self._tables: list[dict] = []
        self._relations: list[dict] = []
        self._load()
        # 新语义层：默认全局单例，可注入便于测试
        self._semantic_layer = semantic_layer or get_semantic_layer_provider()

    def _load(self):
        """加载并校验语义元数据配置。"""
        if not os.path.exists(self.config_path):
            self._tables = []
            self._relations = []
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("semantic_metadata.json 必须是对象，包含 tables 和 relations")

        self._tables = raw.get("tables") or []
        all_relations = raw.get("relations") or []

        # 只保留已启用的关系
        self._relations = []
        for rel in all_relations:
            required = ["id", "left_table", "right_table", "left_key",
                         "right_key", "cardinality", "enabled"]
            missing = [f for f in required if f not in rel]
            if missing:
                raise ValueError(
                    f"关系 {rel.get('id', '?')} 缺少必填字段: {', '.join(missing)}"
                )
            if rel["enabled"]:
                self._relations.append(rel)

    # ── JoinPlanner 用 ──

    def get_relations_for_table(self, table_identifier: str) -> list[dict]:
        """获取指定表参与的所有已启用关系。"""
        result = []
        for rel in self._relations:
            if rel["left_table"] == table_identifier or rel["right_table"] == table_identifier:
                result.append(rel)
        return result

    def get_relation_between(
        self, left_table: str, right_table: str
    ) -> list[dict]:
        """获取两张表之间的直接关系（忽略方向）。"""
        result = []
        for rel in self._relations:
            if (rel["left_table"] == left_table and rel["right_table"] == right_table) or \
               (rel["left_table"] == right_table and rel["right_table"] == left_table):
                result.append(rel)
        return result

    def get_table_grain(self, table_identifier: str) -> str:
        """获取指定表的数据粒度。"""
        for t in self._tables:
            if t.get("identifier") == table_identifier:
                return t.get("grain", "")
        return ""

    def get_table_primary_key(self, table_identifier: str) -> list[str]:
        for t in self._tables:
            if t.get("identifier") == table_identifier:
                return t.get("primary_key") or []
        return []

    def get_table_time_field(self, table_identifier: str) -> Optional[str]:
        """获取指定表的时间字段。"""
        for t in self._tables:
            if t.get("identifier") == table_identifier:
                return t.get("time_field")
        return None

    # ── 语义层用（未来扩展）──

    def get_all_tables(self) -> list[dict]:
        """获取所有已定义的表。"""
        return list(self._tables)

    def get_all_enabled_relations(self) -> list[dict]:
        """获取所有已启用关系。"""
        return list(self._relations)

    def reload(self):
        """重新加载配置，用于热更新。"""
        self._load()

    # ── 新语义层（SemanticLayerProvider）直通接口 ──
    # 优先返回语义层 YAML 的权威结果，老 JSON 仅作 fallback。

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
        """获取指定物理表的分区字段（如 pt_dt, pt_platform）。这是程序的唯一真实来源。

        替代依赖向量库猜测字段归属的旧实现。
        """
        return self._semantic_layer.get_partition_fields(full_name)

    def get_table_fields(self, full_name: str) -> list[str]:
        """获取指定物理表的所有字段名列表"""
        return self._semantic_layer.get_table_fields(full_name)

    def is_field_in_table(self, full_name: str, field_name: str) -> bool:
        """精确校验字段是否属于指定物理表（基于语义层 YAML 权威定义）"""
        return self._semantic_layer.is_field_in_table(full_name, field_name)

    def get_table_time_field(self, table_identifier: str) -> Optional[str]:
        """获取表的时间字段（兼容老接口；优先返回新语义层的 partition 第一个非平台字段）"""
        # 老 JSON 优先（语义层兼容期，保留原始定义）
        legacy = None
        for t in self._tables:
            if t.get("identifier") == table_identifier:
                legacy = t.get("time_field")
                break
        if legacy:
            return legacy
        # Fallback：新语义层 partition 中第一个非 pt_platform 字段
        partitions = self._semantic_layer.get_partition_fields(table_identifier)
        for f in partitions:
            if f not in ("pt_platform",):
                return f
        return partitions[0] if partitions else None

    def get_join_contract(self, left_model: str, right_model: str) -> Optional[dict]:
        """获取 join_contracts.yaml 中两表之间的合规 Join 路径"""
        return self._semantic_layer.get_join_contract(left_model, right_model)

    def get_all_join_contracts(self) -> list[dict]:
        return self._semantic_layer.get_all_join_contracts()

    def find_safe_join_path(self, models: list[str]) -> list[dict]:
        """在给定模型集合中寻找合规 Join 边列表（BFS 生成最小生成树）"""
        return self._semantic_layer.find_safe_join_path(models)