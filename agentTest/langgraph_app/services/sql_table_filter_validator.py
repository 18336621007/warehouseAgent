# 多表逐表过滤校验服务，确保每张参与表都有独立的时间或业务过滤条件。
import re

from agentTest.db.hive_guardrails import REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES


_RESERVED_ALIASES = {
    "ON",
    "WHERE",
    "LEFT",
    "RIGHT",
    "INNER",
    "FULL",
    "CROSS",
    "JOIN",
    "GROUP",
    "ORDER",
    "HAVING",
    "LIMIT",
}


def _extract_table_alias(sql: str, table_name: str) -> str:
    """提取物理表在SQL中的别名，未显式声明时返回短表名。"""
    table_parts = table_name.split(".", 1)
    if len(table_parts) == 2:
        database_name, short_table_name = table_parts
        table_pattern = (
            rf"`?{re.escape(database_name)}`?\s*\.\s*"
            rf"`?{re.escape(short_table_name)}`?"
        )
    else:
        short_table_name = table_name
        table_pattern = rf"`?{re.escape(table_name)}`?"

    match = re.search(
        rf"\b(?:FROM|JOIN)\s+{table_pattern}"
        rf"(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
        sql,
        re.IGNORECASE,
    )
    if not match:
        return ""

    alias = match.group(1) or short_table_name
    if alias.upper() in _RESERVED_ALIASES:
        return short_table_name
    return alias


def _extract_predicate_sql(sql: str) -> str:
    """提取所有JOIN ON和WHERE条件，排除SELECT、GROUP BY等非过滤区域。"""
    on_clauses = re.findall(
        r"\bON\s+(.+?)(?=\b(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    where_match = re.search(
        r"\bWHERE\s+(.+?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    where_clause = where_match.group(1) if where_match else ""
    return " ".join(on_clauses + [where_clause])


def _has_real_filter_condition(predicate_sql: str, field_pattern: str) -> bool:
    """区分常量/函数过滤与字段对字段Join，避免把pt_dt等值Join误判为分区过滤。"""
    direct_patterns = (
        rf"{field_pattern}\s+(?:IN\s*\(|BETWEEN\s+|IS\s+(?:NOT\s+)?NULL)",
        rf"{field_pattern}\s*(?:=|<>|!=|>=|<=|>|<)\s*(.+?)(?=\bAND\b|\bOR\b|$)",
    )
    if re.search(direct_patterns[0], predicate_sql, re.IGNORECASE | re.DOTALL):
        return True

    for match in re.finditer(
        direct_patterns[1],
        predicate_sql,
        re.IGNORECASE | re.DOTALL,
    ):
        right_expression = match.group(1).strip().strip("()")
        # 纯字段引用只表示表间对齐，不构成独立过滤条件。
        if re.fullmatch(
            r"`?[A-Za-z_][A-Za-z0-9_]*`?\s*\.\s*`?[A-Za-z_][A-Za-z0-9_]*`?",
            right_expression,
        ):
            continue
        return True
    return False


def validate_table_plan_filters(
    sql: str,
    tables: list[str],
    table_plans: list[dict],
) -> list[str]:
    """校验每张参与表都存在独立过滤计划，且SQL条件中实际使用对应字段。"""
    issues = []
    plan_by_table = {
        table_plan.get("table", ""): table_plan
        for table_plan in (table_plans or [])
        if table_plan.get("table")
    }
    predicate_sql = _extract_predicate_sql(sql)
    multi_table = len(tables) > 1

    for table_name in tables:
        table_plan = plan_by_table.get(table_name)
        if not table_plan:
            issues.append(f"表 {table_name} 缺少独立过滤计划 table_plan")
            continue

        time_field = (table_plan.get("time_field") or "").strip()
        business_filter = (table_plan.get("filters") or "").strip()
        alias = _extract_table_alias(sql, table_name)
        if not alias:
            issues.append(f"表 {table_name} 未出现在SQL的FROM或JOIN中")
            continue

        # 全局必选字段必须在每张表上分别形成真实过滤，字段对字段Join不算过滤。
        required_fields = list(REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES)
        if time_field and time_field not in required_fields:
            required_fields.append(time_field)
        for required_field in required_fields:
            qualified_pattern = rf"\b{re.escape(alias)}\s*\.\s*`?{re.escape(required_field)}`?\b"
            unqualified_pattern = rf"\b`?{re.escape(required_field)}`?\b"
            pattern = qualified_pattern if multi_table else rf"(?:{qualified_pattern}|{unqualified_pattern})"
            if not _has_real_filter_condition(predicate_sql, pattern):
                issues.append(
                    f"表 {table_name} 缺少全局必选过滤条件 {alias}.{required_field}"
                )

        if business_filter:
            # 业务过滤允许SQL增加表别名前缀，因此使用去空格后的包含校验。
            normalized_filter = business_filter.lower().replace(" ", "").replace("`", "")
            normalized_predicates = predicate_sql.lower().replace(" ", "").replace("`", "")
            if normalized_filter not in normalized_predicates:
                issues.append(
                    f"表 {table_name} 缺少独立业务过滤条件 {business_filter}"
                )

    return issues