from agentTest.langchain_app.utils.sql_cleaner import clear_sql
from agentTest.validate.sql_validate import is_read_only_sql, validate_hive_sql
from agentTest.datasource.hive_datasource import HiveDataSource


class SQLQueryTool:
    # SQLQueryTool 是统一的只读 SQL 执行工具：
    # - 对上层屏蔽 Hive / MySQL 等不同数据源差异
    # - 执行前先做 SQL 安全校验
    # - 执行成功后返回标准结构化查询结果

    def __init__(self, datasource):
        # datasource 负责真正的 query 执行，Tool 层只负责安全检查和调用封装
        self.datasource = datasource

    def run(self, args):
        # 执行 SQL 工具主入口：
        # 1. 读取 sql 参数
        # 2. 做基础只读校验
        # 3. Hive 场景执行更严格的 guardrails 校验
        # 4. 调用底层 datasource 真正执行查询
        sql = args.get("sql")
        if not sql:
            raise ValueError("missing sql")

        # 统一清洗 SQL，去掉结尾分号，避免 Hive 解析报错。
        sql = clear_sql(sql)
        sql = sql.strip().rstrip(";").strip()

        # 先做基础只读校验，拦截写入、DDL 等危险语句
        is_valid, message = is_read_only_sql(sql)
        if not is_valid:
            raise ValueError(f"illegal sql: {message}")

        if isinstance(self.datasource, HiveDataSource):
            # Hive 场景启用更严格的 SQL 安全限制，例如白名单库表、LIMIT 等约束
            is_valid, message = validate_hive_sql(sql)
            if not is_valid:
                raise ValueError(f"illegal hive sql: {message}")

        # 底层 datasource 负责真正执行查询并返回结构化结果
        return self.datasource.query(sql)
