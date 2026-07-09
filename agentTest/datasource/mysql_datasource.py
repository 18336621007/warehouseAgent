import pymysql

from agentTest.datasource.base_datasource import BaseDataSource
from agentTest.db.db_config import get_mysql_config


class MySQLDataSource(BaseDataSource):
    # MySQL 数据源实现，负责执行真实只读 SQL 查询

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

    def query(self, sql: str):
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
            columns = [item[0] for item in cursor.description] if cursor.description else []

            return {
                "sql": sql,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
            }
        finally:
            cursor.close()
            conn.close()

    def list_tables(self):
        raise NotImplementedError("MySQLDataSource only handles query execution")

    def describe_table(self, table_name: str):
        raise NotImplementedError("MySQLDataSource only handles query execution")