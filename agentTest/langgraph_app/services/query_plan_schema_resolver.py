# QueryPlan Schema Resolver，负责根据最终确认方案精确加载单表 Schema。
import re

from agentTest.langgraph_app.state.query_plan import validate_query_plan


# 当前 QueryPlan 使用 database.table 保存稳定库表标识
_TABLE_IDENTIFIER_PATTERN = re.compile(
    r"^([a-zA-Z_][a-zA-Z0-9_]*)"
    r"\."
    r"([a-zA-Z_][a-zA-Z0-9_]*)$"
)


def _parse_table_identifier(
    table_identifier: str,
) -> tuple[str, str]:
    """解析当前 QueryPlan 中的完整库表标识。"""
    if not isinstance(table_identifier, str):
        raise ValueError(
            "查询方案中的表标识必须是字符串"
        )

    matched = _TABLE_IDENTIFIER_PATTERN.fullmatch(
        table_identifier.strip()
    )
    if not matched:
        raise ValueError(
            "查询方案中的表必须使用 database.table 格式："
            f"{table_identifier}"
        )

    return matched.group(1), matched.group(2)


def _format_table_schema(
    table_schema: dict,
    sample_values_map: dict = None,
) -> str:
    """将物理 Schema 格式化为 SQL 生成上下文，含字段枚举值提示。"""
    database_name = table_schema.get(
        "database_name",
        "",
    )
    table_name = table_schema.get(
        "table_name",
        "",
    )
    columns = table_schema.get("columns") or []

    lines = [
        f"表名: {database_name}.{table_name}",
        "字段信息:",
    ]

    for column in columns:
        column_name = column.get("name", "")
        data_type = column.get("type", "")
        comment = column.get("comment", "")

        field_line = (
            f"- {column_name} | "
            f"{data_type} | "
            f"{comment}"
        )
        samples = (sample_values_map or {}).get(
            f"{database_name}.{table_name}.{column_name}", []
        )
        if samples:
            field_line += f" | 枚举值: {'、'.join(str(v) for v in samples)}"
        lines.append(field_line)

    return "\n".join(lines)

def _format_joins(joins: list[dict]) -> str:
    """将 Join 边列表格式化为 SQL 生成上下文中的关联说明"""
    if not joins:
        return ""

    lines = ["", "表关联:"]
    for edge in joins:
        left_side = f"{edge['left_table']}.{edge['left_key']}"
        right_side = f"{edge['right_table']}.{edge['right_key']}"
        join_type = edge.get("join_type", "LEFT")
        lines.append(
            f"- {left_side} = {right_side} ({join_type} JOIN)"
        )
    return "\n".join(lines)


class QueryPlanSchemaResolver:
    """根据 confirmed_plan 解析当前单表物理 Schema。"""

    def __init__(self, metadata_provider, sample_values_map: dict = None):
        self.metadata_provider = metadata_provider
        # {db.table.col: [枚举值]}，用于在 schema 上下文中提示枚举，避免模型猜测过滤值
        self.sample_values_map = sample_values_map or {}

    def resolve(self, confirmed_plan: dict) -> dict:
        # Seeker 入口必须再次执行完整方案校验
        plan_errors = validate_query_plan(
            confirmed_plan,
            require_confirmed=True,
        )
        if plan_errors:
            raise ValueError(
                "Seeker 拒绝非法查询方案："
                + "；".join(plan_errors)
            )

        tables = confirmed_plan.get("tables") or []
        if not tables:
            raise ValueError("查询方案未指定任何数据表")

        # ---- 单表：保持原有逻辑完全不变 ----
        if len(tables) == 1:
            return self._resolve_single_table(
                confirmed_plan, tables[0]
            )

        # ---- 多表：逐表加载 Schema + 拼接 Join 信息 ----
        return self._resolve_multi_table(confirmed_plan, tables)

    def _resolve_single_table(
            self, confirmed_plan: dict, primary_table: str
    ) -> dict:
        """单表解析，维持原有逻辑不变"""
        database_name, table_name = (
            _parse_table_identifier(primary_table)
        )

        available_tables = (
            self.metadata_provider.list_tables()
        )

        exact_matches = [
            table
            for table in available_tables
            if (
                    table.get("database_name")
                    == database_name
                    and table.get("table_name")
                    == table_name
            )
        ]
        if len(exact_matches) != 1:
            raise ValueError(
                "确认方案中的物理表不存在或不唯一："
                f"{primary_table}"
            )

        same_name_tables = [
            table
            for table in available_tables
            if table.get("table_name") == table_name
        ]

        # 当前 Provider 只接收裸表名，因此同名表必须拒绝执行
        if len(same_name_tables) > 1:
            same_name_identifiers = [
                (
                    f"{table.get('database_name', '')}."
                    f"{table.get('table_name', '')}"
                )
                for table in same_name_tables
            ]
            raise ValueError(
                "当前元数据接口无法安全区分跨库同名表："
                + ", ".join(same_name_identifiers)
            )

        table_schema = (
            self.metadata_provider.describe_table(
                table_name
            )
        )

        resolved_identifier = (
            f"{table_schema.get('database_name', '')}."
            f"{table_schema.get('table_name', '')}"
        )
        if (
                resolved_identifier.lower()
                != primary_table.lower()
        ):
            raise ValueError(
                "加载到的物理表与确认方案不一致："
                f"expected={primary_table}, "
                f"actual={resolved_identifier}"
            )

        available_fields = {
            column.get("name", "")
            for column in table_schema.get("columns") or []
            if column.get("name")
        }
        confirmed_fields = (
                confirmed_plan.get("fields") or []
        )

        missing_fields = [
            field_name
            for field_name in confirmed_fields
            if field_name not in available_fields
        ]
        if missing_fields:
            raise ValueError(
                f"确认字段不存在于 {primary_table}："
                + ", ".join(missing_fields)
            )

        schema_context = _format_table_schema(
            table_schema,
            sample_values_map=self.sample_values_map,
        )

        return {
            "schema_context": schema_context,
            "table_identifier": primary_table,
            "column_count": len(available_fields),
        }

    def _resolve_multi_table(
            self, confirmed_plan: dict, tables: list[str]
    ) -> dict:
        """多表解析：逐表加载 Schema，拼接 Join 信息"""
        field_sources = confirmed_plan.get("field_sources") or {}
        joins = confirmed_plan.get("joins") or []
        confirmed_fields = confirmed_plan.get("fields") or []

        schema_parts = []
        all_available_fields: dict[str, set[str]] = {}
        total_columns = 0

        for table_identifier in tables:
            database_name, table_name = (
                _parse_table_identifier(table_identifier)
            )

            # 加载该表的完整 Schema
            table_schema = (
                self.metadata_provider.describe_table(
                    table_name
                )
            )

            resolved_identifier = (
                f"{table_schema.get('database_name', '')}."
                f"{table_schema.get('table_name', '')}"
            )
            if (
                    resolved_identifier.lower()
                    != table_identifier.lower()
            ):
                raise ValueError(
                    "加载到的物理表与确认方案不一致："
                    f"expected={table_identifier}, "
                    f"actual={resolved_identifier}"
                )

            schema_parts.append(
                _format_table_schema(
                    table_schema,
                    sample_values_map=self.sample_values_map,
                )
            )

            available_fields = {
                column.get("name", "")
                for column in table_schema.get("columns") or []
                if column.get("name")
            }
            all_available_fields[table_identifier] = available_fields
            total_columns += len(available_fields)

        # 按 field_sources 校验每个字段在对应表中存在
        missing: list[str] = []
        for field_name in confirmed_fields:
            source_table = field_sources.get(field_name)
            if not source_table:
                missing.append(f"{field_name}（未找到来源表）")
                continue
            available = all_available_fields.get(source_table)
            if available is None:
                missing.append(
                    f"{field_name}（来源表 {source_table} 未加载）"
                )
            elif field_name not in available:
                missing.append(
                    f"{field_name}（不存在于 {source_table}）"
                )

        if missing:
            raise ValueError(
                "以下字段校验失败：\n" + "\n".join(missing)
            )

        # 拼接多表 Schema 上下文
        schema_context = "\n\n".join(schema_parts)

        # 拼接 Join 信息
        if joins:
            schema_context += _format_joins(joins)

        return {
            "schema_context": schema_context,
            "table_identifier": ", ".join(tables),
            "column_count": total_columns,
        }