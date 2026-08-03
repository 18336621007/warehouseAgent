# 语义元数据 Provider，统一管理表定义和 Join 关系
# 数据来源：人工配置的 semantic_metadata.json
# Runtime 只返回 enabled=true 的关系
# 后续语义层扩展时，columns 字段直接填入业务指标映射，Provider 接口不变
import json
import os
from typing import Optional


class SemanticMetadataProvider:
    """语义元数据查询服务，为 JoinPlanner 和后续语义层提供统一数据。"""

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__),
                "semantic_metadata.json",
            )
        self.config_path = config_path
        self._tables: list[dict] = []
        self._relations: list[dict] = []
        self._load()

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