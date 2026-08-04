# 允许访问的 Hive 数据库白名单
ALLOWED_DATABASES = [
    "dwd_trip","dwm_trip","ads_trip","dim_trip"
]

# 允许访问的 Hive 表白名单
ALLOWED_TABLES = [
    "dwd_exchange_order_rent_detail_hour",
    "dwm_exchange_order_addition_detail_hour",
    "ads_exchange_platform_operations_report_day",
    "dim_company_snapshot_day",
    "ads_exchange_order_device_info_day"
]

# 允许作为时间/分区过滤条件的字段
PARTITION_FIELDS = [
    "pt_dt",
]


# 是否允许 join
ALLOW_JOIN = True
# 是否允许 AI 推测 Join（semantic_metadata.json 未配置关联关系时，由 LLM 推断 Join 条件）
ALLOW_AI_INFERRED_JOIN = True
# 是否必须Limit
REQUIRE_LIMIT = True
#是否允许with
ALLOW_WITH = True

# 最大返回行数
MAX_RESULT_ROWS = 100
# Hive 查询超时时间，单位秒
QUERY_TIMEOUT_SECONDS = 30

from agentTest.db.sql_ast_guardrails import validate_sql_ast_guardrails

def validate_sql_with_guardrails(sql: str):
    return validate_sql_ast_guardrails(
        sql=sql,
        allowed_tables=ALLOWED_TABLES,
        partition_fields=PARTITION_FIELDS,
    )