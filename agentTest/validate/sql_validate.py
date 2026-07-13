import re

from agentTest.db.hive_guardrails import (
    ALLOWED_HIVE_DATABASES,
    ALLOWED_HIVE_TABLES,
    REQUIRE_LIMIT,
    ALLOW_JOIN,
    ALLOW_WITH,
)

# 定义所有会修改数据或表结构的高危 SQL 关键字
WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "RENAME", "REPLACE", "LOAD", "GRANT", "REVOKE", "SET", "LOCK", "UNLOCK"
}


def _strip_sql_comments(sql: str) -> str:
    # 去掉单行注释和块注释，降低注释干扰判断的风险
    return re.sub(r"--.*$|/\*[\s\S]*?\*/", "", sql, flags=re.MULTILINE)


def is_read_only_sql(sql: str) -> tuple[bool, str]:
    """
    校验 SQL 是否为只读查询语句，拦截写操作、多语句和明显高风险语句。

    Args:
        sql: 待校验的 SQL 字符串

    Returns:
        tuple: (是否只读, 提示信息)
    """
    if not sql or not sql.strip():
        return False, "SQL 语句不能为空"

    clean_sql = _strip_sql_comments(sql).strip()
    if not clean_sql:
        return False, "SQL 语句不能为空"

    # 禁止多语句执行，避免通过分号拼接写操作
    sql_body = clean_sql.rstrip(";").strip()
    if ";" in sql_body:
        return False, "禁止执行多条 SQL 语句"

    upper_sql = sql_body.upper()
    tokens = re.split(r"\s+", upper_sql)
    if not tokens or not tokens[0]:
        return False, "SQL 语句不能为空"

    first_token = tokens[0]

    # 检测是否包含高危写操作关键字
    for token in tokens:
        normalized_token = token.strip("(),")
        if normalized_token in WRITE_KEYWORDS:
            return False, f"SQL 包含高危写操作关键字 [{normalized_token}]，禁止执行"



    # 允许 SELECT 和 WITH 开头的只读查询，兼容 CTE 写法
    if first_token not in {"SELECT", "WITH"}:
        return False, f"仅支持 SELECT / WITH 查询，当前语句以 {first_token} 开头，禁止执行"

    return True, "SQL 为合法只读查询"


def validate_hive_sql(sql: str) -> tuple[bool, str]:
    """
    Hive 场景下的增强安全校验：
    1. 先做基础只读校验
    2. 要求显式 LIMIT
    3. 限制只能访问白名单数据库
    4. 限制只能访问白名单表
    5. 第一阶段默认不允许 JOIN
    6. 可配置是否允许 WITH 查询

    Args:
        sql: 待校验的 Hive SQL

    Returns:
        tuple: (是否通过, 提示信息)
    """
    is_valid, message = is_read_only_sql(sql)
    if not is_valid:
        return False, message

    clean_sql = _strip_sql_comments(sql).strip()
    sql_body = clean_sql.rstrip(";").strip()
    upper_sql = sql_body.upper()

    # 是否允许 WITH 查询由配置控制
    if not ALLOW_WITH and upper_sql.startswith("WITH"):
        return False, "当前阶段 Hive 查询不允许使用 WITH"

    # 第一阶段要求必须显式带 LIMIT
    if REQUIRE_LIMIT and "LIMIT" not in upper_sql:
        return False, "Hive 查询必须显式带 LIMIT"

    # 第一阶段默认不允许 JOIN，避免模型生成高成本联表查询
    if not ALLOW_JOIN and re.search(r"\bJOIN\b", upper_sql):
        return False, "Hive 查询当前阶段不允许使用 JOIN"

    # 限制访问的数据库必须在白名单中
    table_refs = re.findall(r"\b(?:FROM|JOIN)\s+([A-Z0-9_]+)\.([A-Z0-9_]+)", upper_sql)
    for db_name, table_name in table_refs:
        db_name_lower = db_name.lower()
        table_name_lower = table_name.lower()

        if db_name_lower not in ALLOWED_HIVE_DATABASES:
            return False, f"Hive 查询访问了非白名单库: {db_name_lower}"

        # 如果配置了白名单表，则要求只能访问这些表
        if ALLOWED_HIVE_TABLES and table_name_lower not in ALLOWED_HIVE_TABLES:
            return False, f"Hive 查询访问了非白名单表: {table_name_lower}"

    return True, "Hive SQL 校验通过"