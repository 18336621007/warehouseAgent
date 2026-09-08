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
    required_filter_fields: list[str] | None = None,
) -> list[str]:
    """校验每张参与表都存在独立过滤计划，且SQL条件中实际使用对应字段。
    required_filter_fields 覆盖全局默认必选字段（如明细查询无 pt_dt 分区时传时间字段）。"""
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
        alias = _extract_table_alias(sql, table_name)
        if not alias:
            issues.append(f"表 {table_name} 未出现在SQL的FROM或JOIN中")
            continue

        # 全局必选字段必须在每张表上分别形成真实过滤，字段对字段Join不算过滤。
        # 调用方可传入 required_filter_fields 覆盖默认（明细查询无 pt_dt 时用方案时间字段）。
        required_fields = (
            list(required_filter_fields) if required_filter_fields
            else list(REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES)
        )
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

    return issues


def resolve_required_filter_fields(confirmed_plan: dict) -> list[str]:
    """解析逐表必选过滤字段：有 pt_dt 分区的表仍强制 pt_dt；
    无 pt_dt 分区（无分区表或按 pt_platform 等分区）时改用方案业务时间字段，
    避免对不存在 pt_dt 列的表强制 pt_dt 导致 SQL 执行报错。"""
    # 延迟导入语义层 provider，避免模块加载期耦合
    from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider

    tables = confirmed_plan.get("tables") or []
    table = confirmed_plan.get("table", "") or (tables[0] if tables else "")
    time_field = (confirmed_plan.get("time_field") or "").strip() or "pt_dt"
    if not table:
        return list(REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES)
    info = get_semantic_layer_provider().get_physical_table(table)
    # 表不在语义层（如 RAG 兜底表）时保持默认 pt_dt，行为不变
    if info is None:
        return list(REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES)
    if "pt_dt" in (info.get("partition") or []):
        return ["pt_dt"]
    # 无 pt_dt 分区：使用方案确认的业务时间字段（如 create_time）
    return [time_field]
