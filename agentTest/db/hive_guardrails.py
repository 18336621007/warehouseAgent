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
# 缺 join 契约时禁止 AI 推断：未配置关联关系直接告知用户请联系管理员（方案A）
ALLOW_AI_INFERRED_JOIN = False
# 是否必须Limit
REQUIRE_LIMIT = True
#是否允许with
ALLOW_WITH = True

# 最大返回行数（可配置，见 config/advisor.py RESULT_MAX_ROWS）
from agentTest.config.advisor import RESULT_MAX_ROWS
MAX_RESULT_ROWS = RESULT_MAX_ROWS
# Hive 查询超时时间，单位秒
QUERY_TIMEOUT_SECONDS = 30

from agentTest.db.sql_ast_guardrails import validate_sql_ast_guardrails

def is_table_allowed(table_name: str, database_name: str = "") -> bool:
    # 取消白名单：SQL 访问权限交由底层数据库账号控制（Doris/Trino/Hive 账号能访问即可）
    # 仍保留只读 / LIMIT / select* / 分区过滤等安全规则；白名单判定统一放行
    return True


def validate_sql_with_guardrails(sql: str, partition_fields: list[str] | None = None):
    # partition_fields 允许调用方覆盖默认时间/分区字段：
    # 明细查询（无 pt_dt 分区）按方案时间字段校验，普通查询默认只认 pt_dt。
    return validate_sql_ast_guardrails(
        sql=sql,
        allow_table_fn=is_table_allowed,
        partition_fields=partition_fields or PARTITION_FIELDS,
    )