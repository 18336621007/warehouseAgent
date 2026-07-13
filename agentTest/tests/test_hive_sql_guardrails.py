# 该文件用于测试 Hive SQL 安全限制规则，确保主链路接入 Hive 后，
# 只能执行白名单库、带 LIMIT、无 JOIN 的只读查询，拦截高风险语句。
from agentTest.validate.sql_validate import validate_hive_sql


hive_sql_guardrail_cases = {
    "合法单表查询": {
        "sql": "select * from test.agent_order_demo limit 10",
        "expected_valid": True,
    },
    "合法 WITH 查询": {
        "sql": "with t as (select * from test.agent_order_demo limit 10) select * from t limit 10",
        "expected_valid": True,
    },
    "缺少 LIMIT": {
        "sql": "select * from test.agent_order_demo",
        "expected_valid": False,
    },
    "非白名单库": {
        "sql": "select * from prod.order_info limit 10",
        "expected_valid": False,
    },
    "包含 JOIN": {
        "sql": "select * from test.agent_order_demo a join test.user_demo b on a.user_id = b.user_id limit 10",
        "expected_valid": False,
    },
    "写操作 DELETE": {
        "sql": "delete from test.agent_order_demo where order_id = 1",
        "expected_valid": False,
    },
    "写操作 INSERT": {
        "sql": "insert into test.agent_order_demo values (1, 1001, '上海', 88.5, 'paid', '2026-07-01 10:00:00')",
        "expected_valid": False,
    },
    "多语句拼接": {
        "sql": "select * from test.agent_order_demo limit 10; delete from test.agent_order_demo",
        "expected_valid": False,
    },
}


def run_hive_sql_guardrails_tests():
    # 执行 Hive SQL 安全规则测试，防止高风险 SQL 进入 Hive 执行链路
    passed_count = 0
    total_count = len(hive_sql_guardrail_cases)

    print("=" * 60)
    print("开始测试组: hive_sql_guardrails")
    print("=" * 60)

    for case_name, config in hive_sql_guardrail_cases.items():
        sql = config["sql"]
        expected_valid = config["expected_valid"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"SQL: {sql}")

        actual_valid, message = validate_hive_sql(sql)
        print(f"校验结果: valid={actual_valid}, message={message}")

        if actual_valid == expected_valid:
            print(f"[PASS] {case_name}")
            passed_count += 1
        else:
            print(
                f"[FAIL] [{case_name}] 期望 valid={expected_valid}，实际 valid={actual_valid}"
            )

        print()

    print(f"测试组 hive_sql_guardrails 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count