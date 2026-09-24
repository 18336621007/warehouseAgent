import threading

from agentTest.db.hive_guardrails import MAX_RESULT_ROWS, QUERY_TIMEOUT_SECONDS
from agentTest.langchain_app.utils.sql_cleaner import clear_sql
from agentTest.validate.sql_validate import is_read_only_sql, validate_hive_sql
from agentTest.db.hive_guardrails import MAX_RESULT_ROWS, QUERY_TIMEOUT_SECONDS, validate_sql_with_guardrails


class QueryCancelledError(Exception):
    """用户终止本轮生成时由底层 SQL 执行抛出的异常，调用方据此按 aborted 处理。"""


def _sql_should_cancel():
    """判断当前会话是否已被用户要求停止（延迟 import 避免循环依赖）。"""
    try:
        from web.active_requests import ACTIVE_REQUESTS, is_cancel_requested
        from agentTest.langgraph_app.tools.execute_query_tool import _current_conversation_id, _current_request_id
        conv = _current_conversation_id.get()
        req = _current_request_id.get()
        if conv and is_cancel_requested(ACTIVE_REQUESTS, conv, req):
            return True
    except Exception:
        pass
    return False


def run_query_cancellable(datasource, sql, timeout_seconds, max_rows):
    """在独立线程执行底层 datasource 查询，主线程轮询取消标志。

    数据库查询本身是阻塞调用，无法从外部安全中断，这里采用"放弃等待"策略：
    用户点停止后立即抛 QueryCancelledError，让上层停止推进本轮生成。
    """
    _result = {}
    def _run_query():
        try:
            _result["value"] = datasource.query(
                sql=sql,
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
            )
        except Exception as error:
            _result["error"] = error
    _t = threading.Thread(target=_run_query, daemon=True)
    _t.start()
    while _t.is_alive():
        # 每 0.2s 检查一次取消请求，命中后立即停止等待本条 SQL
        if _sql_should_cancel():
            raise QueryCancelledError("SQL 查询已停止：用户已终止本轮生成")
        _t.join(0.2)
    if "error" in _result:
        raise _result["error"]
    return _result.get("value")


# SQL 查询工具，负责统一执行只读 SQL
class SQLQueryTool:
    # SQLQueryTool 是统一的只读 SQL 执行工具：
    # - 对上层屏蔽 Hive / MySQL 等不同数据源差异
    # - 执行前先做 SQL 安全校验
    # - 执行成功后返回标准结构化查询结果

    def __init__(self, datasource, query_timeout_seconds=QUERY_TIMEOUT_SECONDS):
        # datasource 负责真正的 query 执行，Tool 层只负责安全检查和调用封装
        self.datasource = datasource
        self.query_timeout_seconds = query_timeout_seconds
    def run(self, args):
        # 执行 SQL 工具主入口：
        # 1. 读取 sql 参数
        # 2. 做基础只读校验
        # 3. Hive 场景执行更严格的 guardrails 校验
        # 4. 调用底层 datasource 真正执行查询
        sql = args.get("sql")
        if not sql:
            raise ValueError("missing sql")

        # 统一清洗 SQL，去掉结尾分号，避免 Hive 解析报错。
        sql = clear_sql(sql)
        sql = sql.strip().rstrip(";").strip()

        # 先做基础只读校验，拦截写入、DDL 等危险语句
        is_valid, message = is_read_only_sql(sql)
        if not is_valid:
            raise ValueError(f"illegal sql: {message}")

        # 统一安全校验（Hive/Trino/Doris 共用同一套底线：只读 + LIMIT/JOIN 开关 + 白名单 + select * 门禁 + 分区过滤）
        is_valid, message = validate_hive_sql(sql)
        if not is_valid:
            raise ValueError(f"illegal sql: {message}")

        # 再做 AST Guardrails 资源保护校验
        # partition_fields 允许调用方透传方案时间/分区字段（如明细表无 pt_dt 时用 create_time），
        # 未传时回退默认 pt_dt（与 hive_guardrails.PARTITION_FIELDS 一致）。
        partition_fields = args.get("partition_fields")
        is_valid, message = validate_sql_with_guardrails(sql, partition_fields=partition_fields)
        if not is_valid:
            raise ValueError(f"illegal sql: {message}")

        # 底层 datasource 负责真正执行查询并返回结构化结果。
        # 通过可取消执行封装：用户点停止时立即抛 QueryCancelledError，不再等待长 SQL 返回。
        return run_query_cancellable(
            self.datasource,
            sql,
            timeout_seconds=self.query_timeout_seconds,
            max_rows=MAX_RESULT_ROWS,
        )
