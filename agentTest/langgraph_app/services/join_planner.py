# Join 路径规划器：从 semantic_metadata.json 中查找安全 Join 路径
# 不降级到 LLM 推测，只在已配置关系中查找，找不到时安全拒绝
from dataclasses import dataclass, field
from collections import defaultdict
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider


@dataclass
class JoinPlanResult:
    """Join 路径规划结果"""
    success: bool                                           # 是否成功找到路径
    join_edges: list[dict] = field(default_factory=list)    # Join 边列表
    field_sources: dict = field(default_factory=dict)       # 同 CoverageResult
    target_grain: list[str] = field(default_factory=list)   # 查询粒度维度
    missing_relations: list[str] = field(default_factory=list)  # 缺失的关系描述


class JoinPlanner:
    """安全 Join 规划器，仅使用 semantic_metadata.json 中 enabled=true 的关系"""

    def __init__(self, semantic_provider: SemanticMetadataProvider):
        self._provider = semantic_provider

    def plan(
        self,
        needed_tables: list[str],
        field_sources: dict,
    ) -> JoinPlanResult:
        """为多表查询规划 Join 路径"""
        if len(needed_tables) <= 1:
            # 单表，不需要 Join
            return JoinPlanResult(
                success=True,
                join_edges=[],
                field_sources=field_sources,
                target_grain=self._resolve_grain(needed_tables),
            )

        # BFS 找最小生成树，连接所有 needed_tables
        join_edges, unreachable = self._find_join_spanning_tree(needed_tables)

        if unreachable:
            missing = []
            for table in unreachable:
                missing.append(
                    f"表 {table} 与其他表（{', '.join(set(needed_tables) - {table})}）之间未配置关联关系"
                )
            return JoinPlanResult(
                success=False,
                field_sources=field_sources,
                missing_relations=missing,
            )

        return JoinPlanResult(
            success=True,
            join_edges=join_edges,
            field_sources=field_sources,
            target_grain=self._resolve_grain(needed_tables),
        )

    def _find_join_spanning_tree(self, tables: list[str]) -> tuple[list[dict], list[str]]:
        """BFS 构建表的最小连接树，返回 (join_edges, unreachable_tables)"""
        if not tables:
            return [], []

        # 构建邻接表
        adjacency: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for table in tables:
            relations = self._provider.get_relations_for_table(table)
            for rel in relations:
                left = rel["left_table"]
                right = rel["right_table"]
                if left in tables and right in tables:
                    adjacency[left].append((right, rel))
                    adjacency[right].append((left, rel))

        # BFS
        visited: set[str] = set()
        edges: list[dict] = []
        from collections import deque
        queue = deque([tables[0]])
        visited.add(tables[0])

        while queue:
            current = queue.popleft()
            for neighbor, rel in adjacency.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    edges.append(self._normalize_edge(rel))
                    queue.append(neighbor)

        unreachable = [t for t in tables if t not in visited]
        return edges, unreachable

    def _normalize_edge(self, rel: dict) -> dict:
        """标准化关系边，左表始终是被访问表"""
        return {
            "left_table": rel["left_table"],
            "right_table": rel["right_table"],
            "left_key": rel["left_key"],
            "right_key": rel["right_key"],
            "join_type": rel.get("join_type", "LEFT"),
            "cardinality": rel.get("cardinality", ""),
        }

    def _resolve_grain(self, tables: list[str]) -> list[str]:
        """合并多表的查询粒度"""
        grains = []
        for t in tables:
            grain = self._provider.get_table_grain(t)
            if grain:
                grains.append(grain)
        return grains