# 允许访问的 Hive 数据库白名单（来自 agentTest/config/metadata.yaml，单一事实源见 metadata_scope）
from agentTest.db.metadata_scope import get_allowed_databases
from agentTest.db.metadata_scope import get_include_tables
from agentTest.db.metadata_scope import is_allowed_table as _scope_is_allowed_table

# 兼容旧引用：库级白名单与表级 include 条目（可能为 db.table 形式）
ALLOWED_DATABASES = get_allowed_databases()
ALLOWED_TABLES = get_include_tables()

# 允许作为时间/分区过滤条件的字段
PARTITION_FIELDS = [
    "pt_dt",
]

# 所有参与查询的表都必须包含的过滤字段，例如pt_dt分区过滤。
# 空列表表示不启用全局逐表强制过滤。
REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES = [
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

def is_table_allowed(table_name: str, database_name: str = "") -> bool:
    # 统一接入范围判定：配置白名单（metadata_scope）
    if not database_name:
        return _scope_is_allowed_table(table_name, "")
    return _scope_is_allowed_table(table_name, database_name)


def validate_sql_with_guardrails(sql: str):
    return validate_sql_ast_guardrails(
        sql=sql,
        allow_table_fn=is_table_allowed,
        partition_fields=PARTITION_FIELDS,
    )