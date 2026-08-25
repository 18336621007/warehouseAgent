# Join path planner: finds safe Join paths from semantic_metadata.json
# 优先使用新语义层 join_contracts.yaml（更精确的 safe_for / unsafe_for 规则），
# 兼容旧 semantic_metadata.json 中的 enabled=true 关系（fallback）。
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
    """Join planner using semantic_metadata.json + join_contracts.yaml
    优先级
    1) join_contracts.yaml 中的 25 条合规合约（YAML 标了 safe_for/unsafe_for）
    2) 老 semantic_metadata.json 中 enabled=true 的关系（fallback）
    3) ALLOW_AI_INFERRED_JOIN=True 时，允许 AI 推断剩余表关联
    """

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

        # Step 1: 优先尝试 join_contracts（语义层 YAML 权威路径）
        contract_edges = self._provider.find_safe_join_path(needed_tables)
        # 把 join_contracts 的 {"on": [{"left":..,"right":..}]} 转换为统一格式
        contract_edges = [self._convert_contract_to_edge(c) for c in contract_edges]
        reachable_via_contract, missing_via_contract = self._partition_by_reachability(
            needed_tables, contract_edges
        )

        # Step 2: 兼容老 semantic_metadata.json
        legacy_edges, missing_via_legacy = self._find_join_spanning_tree(needed_tables)
        legacy_reachable, _ = self._partition_by_reachability(
            needed_tables, legacy_edges
        )

        # 合并去重后的边（合约优先）
        all_edges = self._merge_edges(contract_edges, legacy_edges)

        # 真正无法联通的表（合约与 legacy 都没有触及）
        reachable_all = set(reachable_via_contract) | set(legacy_reachable)
        unreachable = [t for t in needed_tables if t not in reachable_all]

        if not unreachable:
            return JoinPlanResult(
                success=True,
                join_edges=all_edges,
                field_sources=field_sources,
                target_grain=self._resolve_grain(needed_tables),
            )

        # Step 3: 仍有不可达表
        from agentTest.db.hive_guardrails import ALLOW_AI_INFERRED_JOIN
        missing = [
            f"Table {t} has no configured relation with ({', '.join(set(needed_tables) - {t})})"
            for t in unreachable
        ]
        if ALLOW_AI_INFERRED_JOIN:
            return JoinPlanResult(
                success=True,
                join_edges=all_edges,
                field_sources=field_sources,
                target_grain=self._resolve_grain(needed_tables),
                missing_relations=missing,
                needs_ai_inference=True,
            )
        return JoinPlanResult(
            success=False,
            join_edges=all_edges,
            field_sources=field_sources,
            missing_relations=missing,
        )

    @staticmethod
    def _convert_contract_to_edge(contract: dict) -> dict:
        """把 join_contracts.yaml 的 {"on": [{"left":..,"right":..}, ...]} 转成边格式。

        与老 semantic_metadata.json 的 left_key/right_key 列表对齐，方便 _normalize_edge 处理。
        """
        on_list = contract.get("on") or []
        left_keys = [str(item.get("left", "")) for item in on_list if isinstance(item, dict) and item.get("left")]
        right_keys = [str(item.get("right", "")) for item in on_list if isinstance(item, dict) and item.get("right")]
        return {
            "id": contract.get("id", ""),
            "left_table": contract.get("left_model", ""),
            "right_table": contract.get("right_model", ""),
            "left_key": left_keys,
            "right_key": right_keys,
            "join_type": contract.get("join_type", "join"),
            "cardinality": contract.get("cardinality", ""),
            "safe_for": list(contract.get("safe_for", []) or []),
            "unsafe_for": list(contract.get("unsafe_for", []) or []),
            "version": "semantic_layer_hrb",
            "source": "join_contracts",
        }

    def _merge_edges(self, primary: list[dict], secondary: list[dict]) -> list[dict]:
        """合并边列表并去重（按 id 优先，否则按 left_table+right_table+left_key+right_key）"""
        seen: set[str] = set()
        result: list[dict] = []
        for edge in list(primary) + list(secondary):
            normalized = self._normalize_edge_key(edge)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(self._normalize_edge(edge))
        return result

    @staticmethod
    def _normalize_edge_key(edge: dict) -> str:
        left = edge.get("left_table", "")
        right = edge.get("right_table", "")
        if not left or not right:
            return ""
        left_keys = edge.get("left_key") or []
        right_keys = edge.get("right_key") or []
        return f"{left}|{right}|{'|'.join(left_keys)}|{'|'.join(right_keys)}"

    def _partition_by_reachability(
        self, tables: list[str], edges: list[dict]
    ) -> tuple[set[str], set[str]]:
        """根据 edges 计算从 tables[0] BFS 可达集合，并返回 (reachable, unreachable)"""
        if not tables:
            return set(), set()
        adj: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            left = edge.get("left_table", "")
            right = edge.get("right_table", "")
            if left and right:
                adj[left].append(right)
                adj[right].append(left)
        visited = {tables[0]}
        queue = deque([tables[0]])
        while queue:
            current = queue.popleft()
            for neighbor in adj.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        unreachable = {t for t in tables if t not in visited}
        return visited, unreachable

    def _find_join_spanning_tree(self, tables: list[str]) -> tuple[list[dict], list[str]]:
        """基于 semantic_metadata.json 老关系 BFS 找最小生成树"""
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
        queue: deque = deque([tables[0]])
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
            "safe_for": list(rel.get("safe_for", []) or []),
            "unsafe_for": list(rel.get("unsafe_for", []) or []),
            "source": rel.get("source", "legacy"),
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
