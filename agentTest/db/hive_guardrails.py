# 允许访问的 Hive 数据库白名单
ALLOWED_DATABASES = [
    "test","dwd_trip","dwm_trip","ads_trip"
]

# 允许访问的 Hive 表白名单
ALLOWED_TABLES = [
    "agent_order_demo",
    "dwd_exchange_order_rent_detail_hour",
    "dwm_exchange_order_addition_detail_hour",
    "ads_exchange_platform_operations_report_day"
]

# 允许作为时间/分区过滤条件的字段
PARTITION_FIELDS = [
    "pt_dt",
    "dt",
    "day",
    "date",
    "pay_time",
    "create_time"

]


# 是否允许 join
ALLOW_JOIN = False
# 是否必须Limit
REQUIRE_LIMIT = True
#是否允许with
ALLOW_WITH = False

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