from agentTest.validate.sql_validate import is_read_only_sql
from agentTest.validate.sql_validate import is_read_only_sql, validate_hive_sql
from agentTest.datasource.hive_datasource import HiveDataSource

class SQLQueryTool:
    # 当前阶段先保留类名，内部改为依赖数据源接口

    def __init__(self, datasource):
        self.datasource = datasource

    def run(self, args):
        sql = args.get("sql")
        if not sql:
            raise ValueError("missing sql")

        # 执行前先做 SQL 安全校验，拦截非只读和高风险语句
        is_valid, message = is_read_only_sql(sql)
        if not is_valid:
            raise ValueError(f"illegal sql: {message}")

        #连接hive
        if isinstance(self.datasource, HiveDataSource):
            # Hive 场景启用更严格的 SQL 安全限制
            is_valid, message = validate_hive_sql(sql)
        else:
            # 非 Hive 场景保留基础只读校验
            is_valid, message = is_read_only_sql(sql)

        return self.datasource.query(sql)
