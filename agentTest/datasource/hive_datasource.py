from agentTest.datasource.base_datasource import BaseDataSource
from agentTest.db.hive_config import get_hive_config
from pyhive import hive

class HiveDataSource(BaseDataSource):
    # Hive 数据源实现，后续负责执行真实只读查询

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
        raise NotImplementedError("HiveDataSource only handles query execution")

    def describe_table(self, table_name: str):
        raise NotImplementedError("HiveDataSource only handles query execution")