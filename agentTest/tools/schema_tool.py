import pymysql

from agentTest.db.db_config import get_mysql_config
from agentTest.schema.schema_models import (
    build_column_meta,
    build_table_meta,
    build_table_schema,
)


class SchemaTool:
    # 基于元数据提供者的 schema 工具，向上层统一提供表与字段结构能力

    def __init__(self, meta_provider):
        self.meta_provider = meta_provider

    def list_tables(self):
        return self.meta_provider.list_tables()

    def describe_table(self, table_name: str):
        return self.meta_provider.describe_table(table_name)

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