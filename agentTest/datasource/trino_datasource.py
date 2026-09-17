# Trino 数据源：通过 trino 客户端执行只读查询（主查询引擎）
# 默认 catalog=hive，查询 Hive 数仓表；失败时由执行层降级 Hive 直连兜底
import json
import logging
import time

import trino
from trino import auth

from agentTest.datasource.base_datasource import BaseDataSource
from agentTest.db.trino_config import get_trino_config

# 404 重试退避：给多 coordinator 负载均衡恢复粘滞的时间，避免极端情况连续重复执行查询
RETRY_BACKOFF_SECONDS = 0.5

# 复用 graph 日志命名空间，重试事件与请求链路日志落同一文件，便于线上统计 404 触发频率
_LOGGER = logging.getLogger("sql_mars.langgraph")


def _elapsed_ms(start_ts):
    # 计算从 start_ts 到当前的毫秒耗时
    return int((time.perf_counter() - start_ts) * 1000)


def _log_retry_event(event, sql, attempt, retry_count, elapsed_ms, error=None):
    # 输出结构化重试日志（JSON），sql 截断避免日志过大；event 区分 404 重试与重试恢复成功
    payload = {
        "event": event,
        "attempt": attempt + 1,
        "retry_count": retry_count,
        "elapsed_ms": elapsed_ms,
    }
    if error is not None:
        payload["error"] = str(error)
    if sql:
        payload["sql"] = sql[:200]
    _LOGGER.warning(json.dumps(payload, ensure_ascii=False))


class TrinoDataSource(BaseDataSource):
    # Trino 查询执行器：SSL 连接（SSLVerification=NONE 对应 verify=False）
    engine = "trino"

    def __init__(self):
        self.config = get_trino_config()

    def _get_connection(self):
        # 显式 BasicAuthentication（用户名密码），request_timeout 为 HTTP 请求超时
        # 绕过系统代理（Windows 127.0.0.1:7897 Clash 等）：代理转发 198.18.x 保留网段不稳定，会偶发 404 Query not found
        import requests
        session = requests.Session()
        session.verify = self.config["verify"]
        session.trust_env = False  # 不读环境/系统代理，直连 Trino
        return trino.dbapi.connect(
            host=self.config["host"],
            port=self.config["port"],
            user=self.config["user"],
            catalog=self.config["catalog"],
            http_scheme=self.config["http_scheme"],
            verify=self.config["verify"],
            auth=auth.BasicAuthentication(self.config["user"], self.config["password"]),
            request_timeout=self.config.get("timeout"),
            http_session=session,
        )

    def query(self, sql: str, timeout_seconds=None, max_rows=None):
        # 执行SQL，返回与 Hive 相同的 {sql, columns, rows, row_count} 结构
        # Trino 多 coordinator 负载均衡偶发 404（POST 建查询成功但 GET 状态路由到其他节点），
        # 客户端无法根治，自动重试（新建连接重新发起）恢复，重试耗尽再抛错由上层降级 Hive
        start_ts = time.perf_counter()
        retry_count = 0
        last_error = None
        for attempt in range(3):
            try:
                result = self._query_once(sql, max_rows)
                # 重试后成功：记录恢复事件，便于线上统计 404 重试成功率
                if retry_count > 0:
                    _log_retry_event("trino.retry_success", sql, attempt, retry_count, _elapsed_ms(start_ts))
                return result
            except RuntimeError as error:
                if "404" not in str(error) or attempt >= 2:
                    raise
                # 404：记录重试事件（attempt + 耗时），短暂退避后重试
                retry_count += 1
                _log_retry_event("trino.retry", sql, attempt, retry_count, _elapsed_ms(start_ts), error=error)
                time.sleep(RETRY_BACKOFF_SECONDS)
                last_error = error
        raise RuntimeError(f"Trino SQL 执行失败（重试后仍 404）: {last_error}")

    def _query_once(self, sql: str, max_rows=None):
        # 单次执行查询，返回结构化结果
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
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
            raise RuntimeError(f"Trino SQL 执行失败: {error}")
        finally:
            # 关闭连接时 cancel 也可能触发 404，需容错，避免覆盖上面的主异常
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def list_tables(self):
        raise NotImplementedError("TrinoDataSource only handles query execution")

    def describe_table(self, table_name: str):
        raise NotImplementedError("TrinoDataSource only handles query execution")
