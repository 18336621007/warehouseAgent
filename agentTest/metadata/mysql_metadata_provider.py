import pymysql

from agentTest.db.db_config import get_mysql_config
from agentTest.metadata.base_metadata_provider import BaseMetadataProvider


class MySQLMetadataProvider(BaseMetadataProvider):
    # MySQL 元数据提供者，负责读取当前数据库的表与字段结构信息

    def _get_connection(self):
        db_cfg = get_mysql_config()
        return pymysql.connect(
            host=db_cfg["host"],
            port=db_cfg["port"],
            user=db_cfg["user"],
            password=db_cfg["password"],
            database=db_cfg["database"],
            charset=db_cfg["charset"],
        )

    def list_tables(self):
        """
        列出所有表
        :return: {
                    "table_name": row[0],
                    "table_comment": row[1] or "",
                }
        """
        db_cfg = get_mysql_config()
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            sql = """
            SELECT table_name, table_comment
            FROM information_schema.tables
            WHERE table_schema = %s
            ORDER BY table_name
            """
            cursor.execute(sql, (db_cfg["database"],))
            rows = cursor.fetchall()

            return [
                {
                    "table_name": row[0],
                    "table_comment": row[1] or "",
                }
                for row in rows
            ]
        finally:
            cursor.close()
            conn.close()

    def describe_table(self, table_name: str):
        """
        解释该表
        :param table_name: 表名
        :return: {
                "table_name": 表名,
                "table_comment": 表说明,
                "columns": [
                    {
                        "name": 列名,
                        "type": 类型,
                        "comment": 列说明,
                    }
                ],
        """
        db_cfg = get_mysql_config()
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            table_sql = """
            SELECT table_name, table_comment
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            """
            cursor.execute(table_sql, (db_cfg["database"], table_name))
            table_row = cursor.fetchone()

            if not table_row:
                raise ValueError(f"table not found: {table_name}")

            column_sql = """
            SELECT column_name, data_type, column_comment
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """
            cursor.execute(column_sql, (db_cfg["database"], table_name))
            column_rows = cursor.fetchall()

            return {
                "table_name": table_row[0],
                "table_comment": table_row[1] or "",
                "columns": [
                    {
                        "name": row[0],
                        "type": row[1],
                        "comment": row[2] or "",
                    }
                    for row in column_rows
                ],
            }
        finally:
            cursor.close()
            conn.close()