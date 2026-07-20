from agentTest.datasource.base_datasource import BaseDataSource
from agentTest.db.hive_config import get_hive_config
from pyhive import hive
# Hive 数据源，负责真正执行 SQL 和底层超时控制
class HiveDataSource(BaseDataSource):
    # Hive 数据源实现，后续负责执行真实只读查询

    def __init__(self):
        self.config = get_hive_config()

    def _get_connection(self, timeout_seconds=None):
        return hive.Connection(
            host=self.config["host"],
            port=self.config["port"],
            username=self.config["username"],
            password=self.config["password"],
            database=self.config["database"],
            auth=self.config["auth"],
            timeout=timeout_seconds,
            configuration={
                "hive.query.timeout.seconds": str(timeout_seconds)
            } if timeout_seconds else None,
        )

    def query(self, sql: str, timeout_seconds=None, max_rows=None):
        # 执行SQL，并支持超时和结果截断保护
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(sql)
            columns = [item[0] for item in cursor.description] if cursor.description else []

            # 如果配置了最大返回行数，则只拉取前 N 行结果
            if max_rows is not None:
                rows = cursor.fetchmany(max_rows)
            else:
                rows = cursor.fetchall()

            return {
                "sql": sql,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
            }
        except Exception as error:
            # 包装底层异常，便于上层统一处理
            raise RuntimeError(f"Hive SQL 执行失败: {error}")

        finally:
            cursor.close()
            conn.close()

    def list_tables(self):
        raise NotImplementedError("HiveDataSource only handles query execution")

    def describe_table(self, table_name: str):
        raise NotImplementedError("HiveDataSource only handles query execution")