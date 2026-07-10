from agentTest.tests.test_planner_validation import run_planner_validation_tests
from agentTest.tests.test_tool_validation import run_tool_validation_tests
from agentTest.tests.test_sql_validation import run_sql_validation_tests

import dotenv

dotenv.load_dotenv()

def main():
    total_passed = 0
    total_count = 0

    # passed, count = run_planner_validation_tests()
    # total_passed += passed
    # total_count += count
    #
    # passed, count = run_tool_validation_tests()
    # total_passed += passed
    # total_count += count
    #
    # passed, count = run_sql_validation_tests()
    # total_passed += passed
    # total_count += count
    try:
        from agentTest.tests.test_hive_smoke import run_hive_smoke_tests

        passed, count = run_hive_smoke_tests()
        total_passed += passed
        total_count += count
    except Exception as error:
        print("=" * 60)
        print("[WARN] Hive smoke test 未执行")
        print(f"原因: {error}")
        print("=" * 60)

    print("=" * 60)
    print(f"全部测试完成，通过 {total_passed}/{total_count}")


if __name__ == "__main__":
    main()
