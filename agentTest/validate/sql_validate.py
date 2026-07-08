import re

# 定义所有会修改数据/表结构的高危SQL关键字
WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "RENAME", "REPLACE", "LOAD", "GRANT", "REVOKE", "SET", "LOCK", "UNLOCK"
}


def is_read_only_sql(sql: str) -> tuple[bool, str]:
    """
    校验SQL是否为纯只读查询语句，拦截所有修改库/表/数据的操作
    Args:
        sql: 待校验的SQL字符串
    Returns:
        tuple: (是否只读, 提示信息)
    """
    if not sql or not sql.strip():
        return False, "SQL语句不能为空"

    # 统一转大写，去除注释、首尾空格
    clean_sql = re.sub(r"--.*$|/\*[\s\S]*?\*/", "", sql.strip().upper())
    # 按空白分割提取关键字
    tokens = re.split(r"\s+", clean_sql)

    # 仅允许SELECT开头的查询，同时禁止写关键字
    first_token = tokens[0]
    if first_token != "SELECT":
        return False, f"仅支持SELECT查询，当前语句以 {first_token} 开头，禁止执行"

    # 遍历检测是否包含写操作关键字
    for token in tokens:
        if token in WRITE_KEYWORDS:
            return False, f"SQL包含高危写操作关键字 [{token}]，禁止执行"

    return True, "SQL为合法只读查询"