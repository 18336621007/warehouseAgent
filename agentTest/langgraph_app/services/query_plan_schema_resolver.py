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
) -> str:
    """将物理 Schema 格式化为 SQL 生成上下文。"""
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

        lines.append(
            f"- {column_name} | "
            f"{data_type} | "
            f"{comment}"
        )

    return "\n".join(lines)


class QueryPlanSchemaResolver:
    """根据 confirmed_plan 解析当前单表物理 Schema。"""

    def __init__(self, metadata_provider):
        self.metadata_provider = metadata_provider

    def resolve(
        self,
        confirmed_plan: dict,
    ) -> dict:
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
        primary_table = confirmed_plan.get(
            "table",
            "",
        )

        # 当前上线版本明确限制为单表查询
        if len(tables) != 1:
            raise ValueError(
                "当前上线版本仅支持单表查询"
            )

        if primary_table != tables[0]:
            raise ValueError(
                "查询方案中的 table 与 tables 不一致"
            )

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

        # 只有库表身份唯一时才调用现有 Provider
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

        # filters 仍是字符串，当前先加载确认表完整物理结构
        schema_context = _format_table_schema(
            table_schema
        )

        return {
            "schema_context": schema_context,
            "table_identifier": primary_table,
            "column_count": len(available_fields),
        }