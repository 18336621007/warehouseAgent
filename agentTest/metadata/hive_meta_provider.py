import copy
from collections import Counter

from agentTest.db.hive_config import get_hive_config
from agentTest.db.hive_guardrails import is_table_allowed
from agentTest.db.metadata_scope import get_allowed_databases
from agentTest.db.metadata_scope import is_allowed_table as _scope_is_allowed_table
from agentTest.metadata.base_metadata_provider import BaseMetadataProvider
from pyhive import hive


class HiveMetadataProvider(BaseMetadataProvider):
    # Hive 元数据提供者，负责读取指定库下的表和字段结构信息，拿到原始 metadata 信息

    def __init__(self):
        self.config = get_hive_config()
        self._tables_cache = None
        self._table_schema_cache = {}

        #测试cache用
        self._list_tables_query_cnt = 0
        self._describe_table_query_cnt = 0

    def _get_connection(self):
        return hive.Connection(
            host=self.config["host"],
            port=self.config["port"],
            username=self.config["username"],
            password=self.config["password"],
            database=self.config["database"],
            auth=self.config["auth"]
        )

    def _is_allowed_table(self, table_name: str, database_name: str = ""):
        # 统一接入范围判定：配置白名单（metadata_scope）
        return is_table_allowed(table_name, database_name)

    def list_tables(self, with_comment: bool = False):
        if self._tables_cache is not None:
            return [dict(table) for table in self._tables_cache] #缓存命中返回拷贝，防止缓存被修改

        # 列出所有表
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            self._list_tables_query_cnt  += 1
            all_tables = []

            # 遍历所有白名单库，查询每个库下的表
            for database_name in get_allowed_databases():
                sql = f"show tables in {database_name}"
                cursor.execute(sql)
                rows = cursor.fetchall()

                for row in rows:
                    all_tables.append({
                        "database_name": database_name,
                        "table_name": row[0],
                        "table_comment": "",
                        "table_type": ""
                    })

            # 统计同名表出现库数，用于裸表名白名单条目的唯一性判定（跨库同名需 db.table 精确指定）
            name_occurrences = Counter(table["table_name"] for table in all_tables)

            # 在 metadata 层执行白名单过滤，避免上层拿到非白名单表
            result = [
                table for table in all_tables
                if _scope_is_allowed_table(
                    table["table_name"], table["database_name"], table_name_occurrences=name_occurrences
                )
            ]
            self._tables_cache = result #缓存

            # 可选：逐表 DESCRIBE FORMATTED 拿表备注（表多时较慢，默认关闭）
            if with_comment:
                self._fill_table_comments(cursor)

            return  [dict(table) for table in self._tables_cache]
        finally:
            cursor.close()
            conn.close()

    def _fill_table_comments(self, cursor):
        # 复用同一连接逐表解析表备注，失败静默降级为空
        for table in self._tables_cache or []:
            table_name = table["table_name"]
            database_name = table["database_name"]
            try:
                cursor.execute(f"describe formatted {database_name}.{table_name}")
                rows = cursor.fetchall()
                comment = ""
                for row in rows:
                    parts = [str(x).strip() for x in row if x is not None and str(x).strip()]
                    if not parts:
                        continue
                    # 兼容 Detailed Table Information 的 Comment: 与 Table Parameters 的 comment 键值行
                    if parts[0].rstrip(":") in ("Comment", "comment"):
                        comment = " ".join(parts[1:]).strip()
                        break
                table["table_comment"] = comment
            except Exception:
                continue

    def describe_table(self, table_name: str):
        # 单表结构查询也要做白名单校验，避免绕过 list_tables 直接访问非白名单表
        # 先定位表所属库名：缓存未初始化时先列出全部表
        if self._tables_cache is None:
            self.list_tables()
        database_name = ""
        for table in self._tables_cache:
            if table["table_name"] == table_name:
                database_name = table["database_name"]
                break
        if not database_name or not is_table_allowed(table_name, database_name):
            raise ValueError(f"table not allowed: {table_name}")

        if table_name in self._table_schema_cache:
            return copy.deepcopy(self._table_schema_cache[table_name])

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            self._describe_table_query_cnt += 1
            # 用找到的库名尝试，失败则遍历所有白名单库重试
            last_error = None
            databases_to_try = [database_name] + [db for db in get_allowed_databases() if db != database_name]
            for db_name in databases_to_try:
                try:
                    sql = f"describe {db_name}.{table_name}"
                    cursor.execute(sql)
                    database_name = db_name  # 找到后更新真实库名
                    break
                except Exception as error:
                    last_error = error
                    continue
            else:
                raise last_error

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
            res = {
                "database_name": database_name,
                "table_name": table_name,
                "table_comment": "",
                "table_type": "",
                "columns": columns,
            }
            self._table_schema_cache[table_name] = res
            return copy.deepcopy(self._table_schema_cache[table_name])
        finally:
            cursor.close()
            conn.close()

    def clear_tables_cache(self):
        self._tables_cache = None

    def clear_schema_cache(self):
        self._table_schema_cache = {}

    def clear_cache(self):
        self._tables_cache = None
        self._table_schema_cache = {}