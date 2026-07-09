from agentTest.tests.test_planner_validation import run_planner_validation_tests
from agentTest.tests.test_tool_validation import run_tool_validation_tests
from agentTest.tests.test_sql_validation import run_sql_validation_tests


def main():
    total_passed = 0
    total_count = 0

    passed, count = run_planner_validation_tests()
    total_passed += passed
    total_count += count

    passed, count = run_tool_validation_tests()
    total_passed += passed
    total_count += count

    passed, count = run_sql_validation_tests()
    total_passed += passed
    total_count += count

    print("=" * 60)
    print(f"全部测试完成，通过 {total_passed}/{total_count}")


if __name__ == "__main__":
    main()
