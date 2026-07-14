import pymysql

from agentTest.db.db_config import get_mysql_config
from agentTest.schema.schema_models import (
    build_column_meta,
    build_table_meta,
    build_table_schema,
)

# SchemaTool 是元数据访问适配层，向上层统一提供表列表和表结构能力。
# 它本身不直接处理 Hive / MySQL 连接细节，而是通过注入的 metadata provider
# 屏蔽底层数据源差异，供 schema_context、schema snapshot、RAG 等模块复用。
class SchemaTool:
    """元数据访问适配层，统一提供 list_tables 和 describe_table 能力。"""
    def __init__(self, meta_provider):
        self.meta_provider = meta_provider

    def list_tables(self):
        # 返回当前数据源下课访问的表列表，逻辑由数据源自己实现
        return self.meta_provider.list_tables()

    def describe_table(self, table_name: str):
        # 返回指定表的结构信息，包括字段列表等元数据。逻辑由数据源自己实现
        return self.meta_provider.describe_table(table_name)

    def run(self, args):
        # 统一的工具入口，按 action 分发到 list_tables 或 describe_table。
        # 这样上层如果以“工具调用”形式使用 schema 能力，也能复用同一套接口。
        action = args.get("action")
        if action == "list_tables":
            return self.list_tables()

        if action == "describe_table":
            table_name = args.get("table_name")
            if not table_name:
                raise ValueError("missing table_name")
            return self.describe_table(table_name)

        raise ValueError(f"unsupported schema action: {action}")