from agentTest.tests.test_hive_sql_guardrails import run_hive_sql_guardrails_tests
from agentTest.tests.test_metadata_cache import run_metadata_cache_tests
from agentTest.tests.test_sql_validation import run_sql_validation_tests
from agentTest.tests.test_hive_metadata_provider import run_hive_metadata_provider_tests

import dotenv

dotenv.load_dotenv()

def main():
    total_passed = 0
    total_count = 0

    # 无 Hive 依赖的回归测试：SQL 安全校验
    passed, count = run_hive_sql_guardrails_tests()
    total_passed += passed
    total_count += count

    passed, count = run_sql_validation_tests()
    total_passed += passed
    total_count += count

    # 依赖真实 Hive 的测试（可单独运行，默认跳过）
    # passed, count = run_metadata_cache_tests()
    # total_passed += passed
    # total_count += count

    # passed, count = run_hive_metadata_provider_tests()
    # total_passed += passed
    # total_count += count

    print("=" * 60)
    print(f"全部测试完成，通过 {total_passed}/{total_count}")


if __name__ == "__main__":
    main()
