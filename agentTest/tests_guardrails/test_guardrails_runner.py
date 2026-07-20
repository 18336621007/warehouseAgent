# 该文件用于统一执行 Guardrails 相关测试入口，便于本地一次性验证安全校验链路。
from agentTest.tests_guardrails.test_execution_guardrails import run_execution_guardrails_tests
from agentTest.tests_guardrails.test_sql_ast_guardrails import run_sql_ast_guardrails_tests
from agentTest.tests_guardrails.test_sql_query_tool_guardrails import run_sql_query_tool_guardrails_tests
from agentTest.tests_guardrails.test_validate_sql_node import run_validate_sql_node_tests


def run_guardrails_all_tests():
    # 统一执行 Guardrails 测试入口。
    total_passed = 0
    total_count = 0

    passed, count = run_sql_ast_guardrails_tests()
    total_passed += passed
    total_count += count

    passed, count = run_sql_query_tool_guardrails_tests()
    total_passed += passed
    total_count += count

    passed, count = run_validate_sql_node_tests()
    total_passed += passed
    total_count += count

    passed, count = run_execution_guardrails_tests()
    total_passed += passed
    total_count += count

    print("=" * 60)
    print(f"Guardrails 测试总结果: 通过 {total_passed}/{total_count}")
    print("=" * 60)

    return total_passed, total_count


if __name__ == "__main__":
    run_guardrails_all_tests()




