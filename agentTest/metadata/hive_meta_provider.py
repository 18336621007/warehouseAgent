from agentTest.db.hive_config import get_hive_config
from agentTest.db.hive_guardrails import ALLOWED_HIVE_TABLES
from agentTest.metadata.base_metadata_provider import BaseMetadataProvider
from pyhive import hive


class HiveMetadataProvider(BaseMetadataProvider):
    # Hive 元数据提供者，负责读取指定库下的表和字段结构信息，拿到原始 metadata 信息

    def __init__(self):
        self.config = get_hive_config()

    def _get_connection(self):
        return hive.Connection(
            host=self.config["host"],
            port=self.config["port"],
            username=self.config["username"],
            password=self.config["password"],
            database=self.config["database"],
            auth=self.config["auth"]
        )

    def _is_allowed_table(self, table_name: str):
        # 当配置了表白名单时，只允许访问白名单内的表
        if not ALLOWED_HIVE_TABLES:
            return True
        return table_name in ALLOWED_HIVE_TABLES

    def list_tables(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            sql = f"show tables in {self.config['database']}"
            cursor.execute(sql)
            rows = cursor.fetchall()

            tables = [
                {
                    "database_name": self.config["database"],
                    "table_name": row[0],
                    "table_comment": "",
                    "table_type": ""
                }
                for row in rows
            ]

            # 在 metadata 层执行白名单过滤，避免上层拿到非白名单表
            return [table for table in tables if self._is_allowed_table(table["table_name"])]
        finally:
            cursor.close()
            conn.close()

    def describe_table(self, table_name: str):
        # 单表结构查询也要做白名单校验，避免绕过 list_tables 直接访问非白名单表
        if not self._is_allowed_table(table_name):
            raise ValueError(f"table not allowed: {table_name}")

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            sql = f"describe {self.config['database']}.{table_name}"
            cursor.execute(sql)
            rows = cursor.fetchall()

            columns = []
            for row in rows:
                column_name = row[0] if len(row) > 0 else None
                data_type = row[1] if len(row) > 1 else ""
                comment = row[2] if len(row) > 2 else ""

                # 过滤空行和分区信息等非字段定义段落
                if not column_name:
                    continue
                if str(column_name).startswith("#"):
                    continue

                columns.append({
                    "name": column_name,
                    "type": data_type,
                    "comment": comment or "",
                    "nullable": None,
                    "partition_key": False,
                })

            return {
                "database_name": self.config["database"],
                "table_name": table_name,
                "table_comment": "",
                "table_type": "",
                "columns": columns,
            }
        finally:
            cursor.close()
            conn.close()
