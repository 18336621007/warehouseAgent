# Doris 数据源：通过 MySQL 协议（pymysql）执行只读查询
# 供 data_project 库（天数池等 Doris 专属 ADS 表）使用；元数据仍走 Hive/语义层
import pymysql

from agentTest.datasource.base_datasource import BaseDataSource
from agentTest.db.doris_config import get_doris_config


class DorisDataSource(BaseDataSource):
    # Doris 查询执行器：连接参数来自 .env（DORIS_*），返回与 Hive 相同的结构化结果
    engine = "doris"

    def __init__(self):
        self.config = get_doris_config()

    def _get_connection(self, timeout_seconds=None):
        # read_timeout 兜底防 Doris 无响应时无限等待（可按查询超时覆盖）
        return pymysql.connect(
            host=self.config["host"],
            port=self.config["port"],
            user=self.config["user"],
            password=self.config["password"],
            charset=self.config["charset"],
            connect_timeout=self.config["connect_timeout"],
            read_timeout=timeout_seconds or self.config["read_timeout"],
        )

    def query(self, sql: str, timeout_seconds=None, max_rows=None):
        # 执行SQL，返回与 Hive 相同的 {sql, columns, rows, row_count} 结构
        conn = self._get_connection(timeout_seconds)
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
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
            raise RuntimeError(f"Doris SQL 执行失败: {error}")
        finally:
            conn.close()

    def list_tables(self):
        raise NotImplementedError("DorisDataSource only handles query execution")

    def describe_table(self, table_name: str):
        raise NotImplementedError("DorisDataSource only handles query execution")
