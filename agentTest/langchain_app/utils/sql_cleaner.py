# 简要注释：SQL 清洗工具模块，负责清洗模型生成的 SQL 文本。

import re

def clear_sql(sql: str) -> str:
    if not sql:
        return ""

    cleaned_sql = sql.strip()

    # 简要注释：去掉 markdown SQL 代码块标记。
    cleaned_sql = re.sub(r"^```sql\s*", "", cleaned_sql, flags=re.IGNORECASE)
    cleaned_sql = re.sub(r"^```\s*", "", cleaned_sql)
    cleaned_sql = re.sub(r"\s*```$", "", cleaned_sql)

    # 简要注释：去掉常见解释前缀。
    cleaned_sql = re.sub(r"^下面是SQL语句[:：]?\s*", "", cleaned_sql)
    cleaned_sql = re.sub(r"^SQL[:：]?\s*", "", cleaned_sql, flags=re.IGNORECASE)

    # 简要注释：去掉结尾分号，避免当前 Hive 环境解析问题。
    cleaned_sql = cleaned_sql.rstrip().rstrip(";").strip()

    return cleaned_sql