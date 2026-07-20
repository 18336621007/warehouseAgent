# 该文件用于测试基于 SQL AST 的 Hive Guardrails 规则，确保白名单、LIMIT、分区条件等限制真实生效。
from agentTest.db.hive_guardrails import ALLOWED_TABLES
from agentTest.db.hive_guardrails import PARTITION_FIELDS
from agentTest.db.sql_ast_guardrails import validate_sql_ast_guardrails


sql_ast_guardrails_cases = {
    "合法安全 SQL": {
        "sql": """
        select order_id, count(*) as order_count
        from agent_order_demo
        where pay_time >= date_sub(current_date(), 7)
        group by order_id
        order by order_count desc
        limit 10
        """,
        "expected_valid": True,
    },
    "缺少 LIMIT": {
        "sql": """
        select order_id, count(*) as order_count
        from agent_order_demo
        where pay_time >= date_sub(current_date(), 7)
        group by order_id
        """,
        "expected_valid": False,
        "expected_message_keyword": "limit",
    },
    "使用 select 星号": {
        "sql": """
        select *
        from agent_order_demo
        where pay_time >= date_sub(current_date(), 7)
        limit 10
        """,
        "expected_valid": False,
        "expected_message_keyword": "select *",
    },
    "非白名单表": {
        "sql": """
        select order_id
        from some_other_table
        where pay_time >= date_sub(current_date(), 7)
        limit 10
        """,
        "expected_valid": False,
        "expected_message_keyword": "白名单",
    },
    "缺少时间分区条件": {
        "sql": """
        select order_id
        from agent_order_demo
        limit 10
        """,
        "expected_valid": False,
        "expected_message_keyword": "时间",
    },
}


def run_sql_ast_guardrails_tests():
    # 执行 SQL AST Guardrails 测试，验证 AST 级安全校验是否真实生效。
    passed_count = 0
    total_count = len(sql_ast_guardrails_cases)

    print("=" * 60)
    print("开始测试组: sql_ast_guardrails")
    print("=" * 60)

    for case_name, config in sql_ast_guardrails_cases.items():
        sql = config["sql"]
        expected_valid = config["expected_valid"]
        expected_message_keyword = config.get("expected_message_keyword", "")

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"SQL: {sql.strip()}")

        actual_valid, message = validate_sql_ast_guardrails(
            sql=sql,
            allowed_tables=ALLOWED_TABLES,
            partition_fields=PARTITION_FIELDS,
        )
        print(f"校验结果: valid={actual_valid}, message={message}")

        if actual_valid != expected_valid:
            print(f"[FAIL] [{case_name}] 期望 valid={expected_valid}，实际 valid={actual_valid}")
            print()
            continue

        if expected_message_keyword and expected_message_keyword not in message.lower() and expected_message_keyword not in message:
            print(f"[FAIL] [{case_name}] 期望 message 包含: {expected_message_keyword}，实际 message={message}")
            print()
            continue

        print(f"[PASS] {case_name}")
        passed_count += 1
        print()

    print(f"测试组 sql_ast_guardrails 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
