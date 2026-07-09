import re

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
    # 允许 SELECT 和 WITH 开头的只读查询，兼容 CTE 写法
    if first_token not in {"SELECT", "WITH"}:
        return False, f"仅支持 SELECT / WITH 查询，当前语句以 {first_token} 开头，禁止执行"

    # 检测是否包含高危写操作关键字
    for token in tokens:
        normalized_token = token.strip("(),")
        if normalized_token in WRITE_KEYWORDS:
            return False, f"SQL 包含高危写操作关键字 [{normalized_token}]，禁止执行"

    return True, "SQL 为合法只读查询"
