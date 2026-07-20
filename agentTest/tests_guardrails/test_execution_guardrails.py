# 该文件用于测试执行层防崩逻辑，验证 SQLQueryTool 是否正确传递超时和最大返回行数参数。
from agentTest.db.hive_guardrails import MAX_RESULT_ROWS
from agentTest.db.hive_guardrails import QUERY_TIMEOUT_SECONDS
from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.tools.sql_query_tool import SQLQueryTool


class FakeSafeHiveDataSource(HiveDataSource):
    # 伪造安全 Hive 数据源，仅记录执行参数，不真实访问 Hive。
    def __init__(self):
        self.last_sql = None
        self.last_timeout_seconds = None
        self.last_max_rows = None

    def query(self, sql: str, timeout_seconds=None, max_rows=None):
        # 记录执行参数，验证 Tool 层是否把执行保护参数传递到底层。
        self.last_sql = sql
        self.last_timeout_seconds = timeout_seconds
        self.last_max_rows = max_rows

        rows = []
        for index in range(max_rows or 0):
            rows.append([f"order_{index}"])

        return {
            "sql": sql,
            "columns": ["order_id"],
            "rows": rows,
            "row_count": len(rows),
        }


execution_guardrails_cases = {
    "安全 SQL 应传入统一超时和最大行数": {
        "sql": """
        select order_id
        from agent_order_demo
        where pay_time >= date_sub(current_date(), 7)
        limit 10
        """,
        "expected_timeout_seconds": QUERY_TIMEOUT_SECONDS,
        "expected_max_rows": MAX_RESULT_ROWS,
    },
}


def run_execution_guardrails_tests():
    # 执行执行层防崩测试，验证 Tool 到 DataSource 的超时和最大行数参数传递。
    passed_count = 0
    total_count = len(execution_guardrails_cases)

    print("=" * 60)
    print("开始测试组: execution_guardrails")
    print("=" * 60)

    for case_name, config in execution_guardrails_cases.items():
        datasource = FakeSafeHiveDataSource()
        tool = SQLQueryTool(datasource)

        sql = config["sql"]
        expected_timeout_seconds = config["expected_timeout_seconds"]
        expected_max_rows = config["expected_max_rows"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"SQL: {sql.strip()}")

        result = tool.run({"sql": sql})
        print(f"执行结果: {result}")
        print(f"记录到 datasource 的 timeout_seconds={datasource.last_timeout_seconds}, max_rows={datasource.last_max_rows}")

        if datasource.last_timeout_seconds != expected_timeout_seconds:
            print(f"[FAIL] [{case_name}] 期望 timeout_seconds={expected_timeout_seconds}，实际={datasource.last_timeout_seconds}")
            print()
            continue

        if datasource.last_max_rows != expected_max_rows:
            print(f"[FAIL] [{case_name}] 期望 max_rows={expected_max_rows}，实际={datasource.last_max_rows}")
            print()
            continue

        if result.get("row_count") != expected_max_rows:
            print(f"[FAIL] [{case_name}] 期望 row_count={expected_max_rows}，实际={result.get('row_count')}")
            print()
            continue

        print(f"[PASS] {case_name}")
        passed_count += 1
        print()

    print(f"测试组 execution_guardrails 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
