# 基于 SQL AST 的 Guardrails 规则模块
from sqlglot import exp
from sqlglot import parse_one


def parse_sql_ast(sql: str):
    # 使用 Hive 方言解析 SQL，返回 AST 表达式对象
    return parse_one(sql, read="hive")


def extract_table_names(expression) -> list[str]:
    # 从 AST 中提取 SQL 涉及的表名
    table_names = []

    for table in expression.find_all(exp.Table):
        table_name = table.name
        if table_name:
            table_names.append(table_name.lower())

    return list(set(table_names))


def has_limit(expression) -> bool:
    # 判断 SQL 是否包含 limit
    return expression.args.get("limit") is not None


def has_select_star(expression) -> bool:
    # 判断 SQL 是否使用 select *
    for select_expression in expression.find_all(exp.Select):
        for projection in select_expression.expressions:
            if isinstance(projection, exp.Star):
                return True

            if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                return True

    return False


def has_partition_filter(expression, partition_fields: list[str]) -> bool:
    # 判断 where 条件中是否包含时间/分区字段
    normalized_partition_fields = {field.lower() for field in partition_fields}

    for where_expression in expression.find_all(exp.Where):
        for column in where_expression.find_all(exp.Column):
            column_name = column.name.lower()
            if column_name in normalized_partition_fields:
                return True

    return False


def check_table_whitelist(expression, allowed_tables: list[str]):
    # 校验 SQL 中使用的表是否都在白名单中
    normalized_allowed_tables = {table.lower() for table in allowed_tables}
    used_tables = extract_table_names(expression)

    for table_name in used_tables:
        if table_name not in normalized_allowed_tables:
            return False, f"SQL 使用了非白名单表: {table_name}"

    return True, ""


def validate_sql_ast_guardrails(
    sql: str,
    allowed_tables: list[str],
    partition_fields: list[str],
):
    # 统一执行 AST 级 Guardrails 校验
    try:
        expression = parse_sql_ast(sql)
    except Exception as error:
        return False, f"SQL AST 解析失败: {error}"

    is_valid, message = check_table_whitelist(expression, allowed_tables)
    if not is_valid:
        return False, message

    if not has_limit(expression):
        return False, "SQL 未包含 limit，存在大结果集风险"

    if has_select_star(expression):
        return False, "SQL 使用了 select *，存在宽表全列扫描风险"

    if not has_partition_filter(expression, partition_fields):
        return False, "SQL 未包含时间/分区过滤条件，存在全表扫描风险"

    return True, ""