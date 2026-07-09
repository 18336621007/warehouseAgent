import pymysql

from agentTest.db.db_config import get_mysql_config
from agentTest.schema.schema_models import (
    build_column_meta,
    build_table_meta,
    build_table_schema,
)


class SchemaTool:
    # 当前阶段改为基于数据源接口获取 schema 信息

    def __init__(self, datasource):
        self.datasource = datasource

    def list_tables(self):
        return self.datasource.list_tables()

    def describe_table(self, table_name: str):
        return self.datasource.describe_table(table_name)

    def run(self, args):
        action = args.get("action")
        if action == "list_tables":
            return self.list_tables()

        if action == "describe_table":
            table_name = args.get("table_name")
            if not table_name:
                raise ValueError("missing table_name")
            return self.describe_table(table_name)

        raise ValueError(f"unsupported schema action: {action}")