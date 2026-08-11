# 基于 SQL AST 的 Guardrails 规则模块
from sqlglot import exp
from sqlglot import parse_one
from sqlglot import exp
from sqlglot import parse_one
from sqlglot import parse          # 新增：解析多条语句

# AST 中代表写操作/DDL 的节点类型
WRITE_EXP_TYPES = {
    exp.Insert,
    exp.Delete,
    exp.Update,
    exp.Drop,
    exp.Create,
    exp.Merge,
}


def is_read_only_ast(sql: str):
    # 基于 AST 判断 SQL 是否只读，拦截非 SELECT/WITH 语句、写操作和多语句
    if not sql or not sql.strip():
        return False, "SQL 语句不能为空"

    # 解析多条语句，检测分号拼接攻击
    statements = list(parse(sql, read="hive"))
    if len(statements) > 1:
        return False, "禁止执行多条 SQL 语句"

    # 解析为单条 AST
    try:
        expression = parse_one(sql, read="hive")
    except Exception as error:
        return False, f"SQL 解析失败: {error}"

    # 只允许 SELECT 开头（WITH 在 sqlglot 中也解析为 Select）
    if not isinstance(expression, exp.Select):
        return False, "仅支持 SELECT / WITH 查询"

    # 递归检查 AST 中是否包含写操作节点
    for write_type in WRITE_EXP_TYPES:
        if expression.find(write_type):
            return False, f"SQL 包含高危写操作，禁止执行"

    return True, "SQL 为合法只读查询"


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


def check_table_whitelist(expression, allow_table_fn=None, allowed_tables: list[str] | None = None):
    # 校验 SQL 中使用的表是否都在白名单中（支持 db.table 精确判定）
    used_tables = []
    for table in expression.find_all(exp.Table):
        db_name = (table.db or "").lower()
        table_name = (table.name or "").lower()
        if table_name:
            used_tables.append((db_name, table_name))

    for db_name, table_name in used_tables:
        if allow_table_fn is not None:
            if not allow_table_fn(table_name, db_name):
                full = f"{db_name}.{table_name}" if db_name else table_name
                return False, f"SQL 使用了非白名单表: {full}"
        else:
            normalized = {t.lower() for t in (allowed_tables or [])}
            candidate = f"{db_name}.{table_name}" if db_name else table_name
            if table_name not in normalized and candidate not in normalized:
                return False, f"SQL 使用了非白名单表: {candidate}"

    return True, ""


def validate_sql_ast_guardrails(
    sql: str,
    allow_table_fn=None,
    allowed_tables: list[str] | None = None,
    partition_fields: list[str] | None = None,
):
    # 1. 先做只读校验（AST 版）
    is_valid, message = is_read_only_ast(sql)
    if not is_valid:
        return False, message

    # # 2. 解析 AST
    try:
        expression = parse_sql_ast(sql)
    except Exception as error:
        return False, f"SQL AST 解析失败: {error}"

    is_valid, message = check_table_whitelist(expression, allow_table_fn=allow_table_fn, allowed_tables=allowed_tables)
    if not is_valid:
        return False, message

    if not has_limit(expression):
        return False, "SQL 未包含 limit，存在大结果集风险"

    if has_select_star(expression):
        return False, "SQL 使用了 select *，存在宽表全列扫描风险"

    if not has_partition_filter(expression, partition_fields or ["pt_dt"]):
        return False, "SQL 未包含时间/分区过滤条件，存在全表扫描风险"

    return True, ""