# 表覆盖分析器：将 confirmed_plan 的字段映射到物理表，判断单表能否覆盖
# 使用列向量库查找每个字段所属的表，返回 CoverageResult
from dataclasses import dataclass, field


@dataclass
class CoverageResult:
    """覆盖分析结果，描述字段到表的映射关系和覆盖状态"""
    single_table: bool                      # 单表是否能覆盖所有字段
    field_sources: dict                     # {字段名: database.table}
    needed_tables: list[str]                # 去重后的参与表列表
    uncovered_fields: list[str] = field(default_factory=list)  # 未找到映射的字段


class TableCoverageAnalyzer:
    """字段覆盖分析器，通过列向量库查找字段所属的物理表"""

    def __init__(self, column_vector_store, metadata_provider):
        self._column_vector_store = column_vector_store
        self._metadata_provider = metadata_provider

    def analyze(self, confirmed_plan: dict) -> CoverageResult:
        """分析 confirmed_plan 中的字段分别属于哪些表"""
        fields = confirmed_plan.get("fields") or []
        primary_table = confirmed_plan.get("table", "")

        field_sources: dict[str, str] = {}
        uncovered: list[str] = []

        for field_name in fields:
            table_found = self._find_field_table(
                field_name, primary_table=primary_table
            )
            if table_found:
                field_sources[field_name] = table_found
            else:
                uncovered.append(field_name)

        needed_tables = list(dict.fromkeys(field_sources.values()))
        single_table = len(needed_tables) <= 1

        return CoverageResult(
            single_table=single_table,
            field_sources=field_sources,
            needed_tables=needed_tables if needed_tables else [primary_table],
            uncovered_fields=uncovered,
        )

    def _find_field_table(self, field_name: str, primary_table: str = "") -> str:
        """通过列向量库定位字段所属的表，优先匹配主表内的字段"""
        # 先尝试在主表范围内检索
        if primary_table:
            main_docs = self._column_vector_store.similarity_search(
                field_name, k=3,
                filter={"table": primary_table},
            )
            for doc in main_docs:
                col = doc.metadata.get("column", "")
                if col == field_name:
                    return primary_table

        # 全域检索
        docs = self._column_vector_store.similarity_search(
            field_name, k=5,
        )
        for doc in docs:
            col = doc.metadata.get("column", "")
            table = doc.metadata.get("table", "")
            if col == field_name and table:
                return table

        return ""