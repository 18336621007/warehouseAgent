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
        declared_sources = confirmed_plan.get("field_sources") or {}

        # 确认方案涉及的表集合：未声明来源的字段只在方案内定位，禁止全域检索绑到方案外的表
        plan_tables = set(confirmed_plan.get("tables") or [])
        if primary_table:
            plan_tables.add(primary_table)

        field_sources: dict[str, str] = {}
        uncovered: list[str] = []

        for field_ref in fields:
            # 兼容完整路径 "db.table.field"：拆出裸字段名和字段自带来源表
            if isinstance(field_ref, str) and field_ref.count(".") >= 2:
                table_part, _, field_name = field_ref.rpartition(".")
                implicit_table = table_part
            else:
                field_name = str(field_ref)
                implicit_table = ""

            # 来源优先级：field_sources 显式来源 > 字段自带表前缀 > 方案作用域内检索
            declared_table = declared_sources.get(field_name, "") or implicit_table
            if declared_table:
                # 已锁定的字段来源只能做精确校验，禁止执行阶段静默换表。
                if self._field_exists_in_table(field_name, declared_table):
                    field_sources[field_name] = declared_table
                else:
                    uncovered.append(field_name)
                continue

            table_found = self._find_field_table_in_scope(
                field_name, plan_tables=plan_tables
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

    def _field_exists_in_table(self, field_name: str, table_name: str) -> bool:
        """精确校验字段是否属于已锁定的物理表。
        直接查 docstore 做 metadata 精确匹配，避免向量检索 fetch_k 截断导致漏检。"""
        columns = self._column_vector_store.columns_in_table(table_name)
        return field_name in columns

    def _find_field_table_in_scope(self, field_name: str, plan_tables: set) -> str:
        """在确认方案涉及的表集合内按裸字段名精确检索来源表。
        只允许绑定到方案内的表，避免全域检索把字段绑到方案外的表导致参与表被错误扩充。"""
        for table_name in plan_tables:
            if self._field_exists_in_table(field_name, table_name):
                return table_name
        return ""