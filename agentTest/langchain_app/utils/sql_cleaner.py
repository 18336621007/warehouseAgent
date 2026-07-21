# 简要注释：SQL 清洗工具模块，负责清洗模型生成的 SQL 文本。
import re

from agentTest.semantic.semantic_rules import PARTITION_FIELD_FORMATS

def fix_partition_date_format(sql: str) -> str:
    # 只对 yyyyMMdd 格式的字段做清洗，yyyy-MM-dd 格式的字段不需要转换
    for field, fmt in PARTITION_FIELD_FORMATS.items():
        if fmt != "yyyyMMdd":
            continue
        # pt_dt = date_sub(current_date, N) -> pt_dt = regexp_replace(date_sub(...), '-', '')
        sql = re.sub(
            rf"{field}\s*(=|>=|<=)\s*date_sub\s*\(\s*current_date\s*,\s*(\d+)\s*\)",
            rf"{field} \1 regexp_replace(date_sub(current_date, \2), '-', '')",
            sql, flags=re.IGNORECASE,
        )
        # pt_dt = date_format(date_sub(...), ...) -> 同上
        sql = re.sub(
            rf"{field}\s*(=|>=|<=)\s*date_format\s*\(\s*date_sub\s*\(\s*current_date\s*,\s*(\d+)\s*\)\s*,\s*'[^']*'\s*\)",
            rf"{field} \1 regexp_replace(date_sub(current_date, \2), '-', '')",
            sql, flags=re.IGNORECASE,
        )
    return sql

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

    #分区格式
    fixed_sql = fix_partition_date_format(cleaned_sql)

    return fixed_sql