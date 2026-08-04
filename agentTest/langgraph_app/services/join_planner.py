# Join path planner: finds safe Join paths from semantic_metadata.json
# Supports ALLOW_AI_INFERRED_JOIN switch: allows AI-inferred joins when relations not configured
from dataclasses import dataclass, field
from collections import defaultdict, deque
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider


@dataclass
class JoinPlanResult:
    """Join plan result"""
    success: bool                                           # Whether path found successfully
    join_edges: list[dict] = field(default_factory=list)    # Join edge list
    field_sources: dict = field(default_factory=dict)       # Same as CoverageResult
    target_grain: list[str] = field(default_factory=list)   # Query grain dimensions
    missing_relations: list[str] = field(default_factory=list)  # Missing relation descriptions
    needs_ai_inference: bool = False                        # Whether AI needs to infer Join conditions


class JoinPlanner:
    """Join planner using semantic_metadata.json enabled=true relations, supports AI inference fallback"""

    def __init__(self, semantic_provider: SemanticMetadataProvider):
        self._provider = semantic_provider

    def plan(
        self,
        needed_tables: list[str],
        field_sources: dict,
    ) -> JoinPlanResult:
        """Plan Join paths for multi-table queries"""
        if len(needed_tables) <= 1:
            # Single table, no Join needed
            return JoinPlanResult(
                success=True,
                join_edges=[],
                field_sources=field_sources,
                target_grain=self._resolve_grain(needed_tables),
            )

        # BFS to find minimum spanning tree connecting all needed_tables
        join_edges, unreachable = self._find_join_spanning_tree(needed_tables)

        if unreachable:
            from agentTest.db.hive_guardrails import ALLOW_AI_INFERRED_JOIN
            missing = []
            for table in unreachable:
                others = set(needed_tables) - {table}
                missing.append(
                    f"Table {table} has no configured relation with ({', '.join(others)})"
                )
            if ALLOW_AI_INFERRED_JOIN:
                # Allow AI inference: return success but mark as needing inference
                return JoinPlanResult(
                    success=True,
                    join_edges=join_edges,
                    field_sources=field_sources,
                    target_grain=self._resolve_grain(needed_tables),
                    missing_relations=missing,
                    needs_ai_inference=True,
                )
            else:
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
        """BFS to build minimum spanning tree, returns (join_edges, unreachable_tables)"""
        if not tables:
            return [], []

        # Build adjacency list
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
        """Normalize relation edge"""
        left_keys = self._normalize_keys(rel["left_key"])
        right_keys = self._normalize_keys(rel["right_key"])
        if len(left_keys) != len(right_keys):
            raise ValueError(f"Join关系 {rel.get('id', '?')} 左右键数量不一致")

        return {
            "left_table": rel["left_table"],
            "right_table": rel["right_table"],
            "left_key": left_keys,
            "right_key": right_keys,
            "join_type": rel.get("join_type", "LEFT"),
            "cardinality": rel.get("cardinality", ""),
            "version": rel.get("version", ""),
        }

    @staticmethod
    def _normalize_keys(keys) -> list[str]:
        """统一单字段和复合字段Join键格式。"""
        if isinstance(keys, str):
            return [keys]
        return [key for key in (keys or []) if isinstance(key, str) and key]

    def _resolve_grain(self, tables: list[str]) -> list[str]:
        """Merge grain dimensions across tables"""
        grains = []
        for t in tables:
            grain = self._provider.get_table_grain(t)
            if grain:
                grains.append(grain)
        return grains
