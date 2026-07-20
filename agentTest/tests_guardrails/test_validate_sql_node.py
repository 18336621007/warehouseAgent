# 该文件用于测试 LangGraph 的 validate_sql_node，确保节点层会正确接入基础校验和 Guardrails 校验。
from agentTest.langgraph_app.nodes.validate_sql_node import validate_sql_node


validate_sql_node_cases = {
    "安全 SQL 可以通过": {
        "state": {
            "generated_sql": """
            select order_id
            from agent_order_demo
            where pay_time >= date_sub(current_date(), 7)
            limit 10
            """
        },
        "expected_sql_valid": True,
        "expected_error_keyword": "",
    },
    "缺少 LIMIT 会失败": {
        "state": {
            "generated_sql": """
            select order_id
            from agent_order_demo
            where pay_time >= date_sub(current_date(), 7)
            """
        },
        "expected_sql_valid": False,
        "expected_error_keyword": "limit",
    },
    "select 星号会失败": {
        "state": {
            "generated_sql": """
            select *
            from agent_order_demo
            where pay_time >= date_sub(current_date(), 7)
            limit 10
            """
        },
        "expected_sql_valid": False,
        "expected_error_keyword": "select *",
    },
    "非白名单表会失败": {
        "state": {
            "generated_sql": """
            select order_id
            from some_other_table
            where pay_time >= date_sub(current_date(), 7)
            limit 10
            """
        },
        "expected_sql_valid": False,
        "expected_error_keyword": "白名单",
    },
    "缺少时间条件会失败": {
        "state": {
            "generated_sql": """
            select order_id
            from agent_order_demo
            limit 10
            """
        },
        "expected_sql_valid": False,
        "expected_error_keyword": "时间",
    },
}


def run_validate_sql_node_tests():
    # 执行 validate_sql_node 测试，验证节点层是否正确返回 sql_valid 和 sql_error。
    passed_count = 0
    total_count = len(validate_sql_node_cases)

    print("=" * 60)
    print("开始测试组: validate_sql_node")
    print("=" * 60)

    for case_name, config in validate_sql_node_cases.items():
        state = config["state"]
        expected_sql_valid = config["expected_sql_valid"]
        expected_error_keyword = config["expected_error_keyword"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"state: {state}")

        result = validate_sql_node(state)
        actual_sql_valid = result.get("sql_valid")
        actual_sql_error = result.get("sql_error", "")

        print(f"节点结果: {result}")

        if actual_sql_valid != expected_sql_valid:
            print(f"[FAIL] [{case_name}] 期望 sql_valid={expected_sql_valid}，实际 sql_valid={actual_sql_valid}")
            print()
            continue

        if expected_error_keyword:
            if expected_error_keyword not in actual_sql_error.lower() and expected_error_keyword not in actual_sql_error:
                print(f"[FAIL] [{case_name}] 期望 sql_error 包含: {expected_error_keyword}，实际 sql_error={actual_sql_error}")
                print()
                continue

        print(f"[PASS] {case_name}")
        passed_count += 1
        print()

    print(f"测试组 validate_sql_node 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
