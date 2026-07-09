import pymysql

from agentTest.db.db_config import get_mysql_config
from agentTest.schema.schema_models import (
    build_column_meta,
    build_table_meta,
    build_table_schema,
)


class SchemaTool:
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
                build_table_meta(table_name=row[0], table_comment=row[1])
                for row in rows
            ]
        finally:
            cursor.close()
            conn.close()

    def describe_table(self, table_name: str):
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

            columns = [
                build_column_meta(name=row[0], data_type=row[1], comment=row[2])
                for row in column_rows
            ]
            return build_table_schema(
                table_name=table_row[0],
                table_comment=table_row[1],
                columns=columns,
            )
        finally:
            cursor.close()
            conn.close()

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
