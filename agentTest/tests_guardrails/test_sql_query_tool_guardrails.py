# 该文件用于测试 SQLQueryTool 是否真正接入了 Guardrails，并在执行前拦截高风险 SQL。
from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.tools.sql_query_tool import SQLQueryTool


class FakeHiveDataSource(HiveDataSource):
    # 伪造 Hive 数据源，用于验证 Tool 是否调用了底层 query。
    def __init__(self):
        self.last_sql = None
        self.last_timeout_seconds = None
        self.last_max_rows = None

    def query(self, sql: str, timeout_seconds=None, max_rows=None):
        # 记录执行参数，避免真实访问 Hive。
        self.last_sql = sql
        self.last_timeout_seconds = timeout_seconds
        self.last_max_rows = max_rows

        return {
            "sql": sql,
            "columns": ["order_id"],
            "rows": [["A001"]],
            "row_count": 1,
        }


tool_guardrails_cases = {
    "安全 SQL 可以执行": {
        "sql": """
        select order_id
        from agent_order_demo
        where pay_time >= date_sub(current_date(), 7)
        limit 10
        """,
        "expected_success": True,
    },
    "缺少 LIMIT 会被拦截": {
        "sql": """
        select order_id
        from agent_order_demo
        where pay_time >= date_sub(current_date(), 7)
        """,
        "expected_success": False,
        "expected_message_keyword": "limit",
    },
    "select 星号会被拦截": {
        "sql": """
        select *
        from agent_order_demo
        where pay_time >= date_sub(current_date(), 7)
        limit 10
        """,
        "expected_success": False,
        "expected_message_keyword": "select *",
    },
    "非白名单表会被拦截": {
        "sql": """
        select order_id
        from some_other_table
        where pay_time >= date_sub(current_date(), 7)
        limit 10
        """,
        "expected_success": False,
        "expected_message_keyword": "白名单",
    },
    "缺少时间条件会被拦截": {
        "sql": """
        select order_id
        from agent_order_demo
        limit 10
        """,
        "expected_success": False,
        "expected_message_keyword": "时间",
    },
}


def run_sql_query_tool_guardrails_tests():
    # 执行 SQLQueryTool Guardrails 测试，验证执行入口是否真正拦截危险 SQL。
    passed_count = 0
    total_count = len(tool_guardrails_cases)

    print("=" * 60)
    print("开始测试组: sql_query_tool_guardrails")
    print("=" * 60)

    for case_name, config in tool_guardrails_cases.items():
        datasource = FakeHiveDataSource()
        tool = SQLQueryTool(datasource)

        sql = config["sql"]
        expected_success = config["expected_success"]
        expected_message_keyword = config.get("expected_message_keyword", "")

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"SQL: {sql.strip()}")

        try:
            result = tool.run({"sql": sql})
            actual_success = True
            actual_message = ""
            print(f"执行结果: {result}")
        except Exception as error:
            actual_success = False
            actual_message = str(error)
            print(f"执行异常: {actual_message}")

        if actual_success != expected_success:
            print(f"[FAIL] [{case_name}] 期望 success={expected_success}，实际 success={actual_success}")
            print()
            continue

        if not expected_success:
            if expected_message_keyword and expected_message_keyword not in actual_message.lower() and expected_message_keyword not in actual_message:
                print(f"[FAIL] [{case_name}] 期望错误信息包含: {expected_message_keyword}，实际 error={actual_message}")
                print()
                continue
        else:
            if datasource.last_sql is None:
                print(f"[FAIL] [{case_name}] 安全 SQL 未真正调用到底层 datasource.query")
                print()
                continue

        print(f"[PASS] {case_name}")
        passed_count += 1
        print()

    print(f"测试组 sql_query_tool_guardrails 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
