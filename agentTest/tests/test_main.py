from agentTest.tests.test_agent_schema_rag import run_agent_schema_rag_tests
from agentTest.tests.test_hive_sql_guardrails import run_hive_sql_guardrails_tests
from agentTest.tests.test_metadata_cache import run_metadata_cache_tests
from agentTest.tests.test_planner_prompt_schema_rag import run_planner_prompt_schema_rag_tests
from agentTest.tests.test_planner_validation import run_planner_validation_tests
from agentTest.tests.test_schema_context import run_schema_context_tests
from agentTest.tests.test_schema_snapshot_cache import run_schema_snapshot_cache_tests
from agentTest.tests.test_tool_validation import run_tool_validation_tests
from agentTest.tests.test_sql_validation import run_sql_validation_tests
from agentTest.tests.test_hive_metadata_provider import run_hive_metadata_provider_tests
from agentTest.tests.test_hive_schema_documents import run_hive_schema_documents_tests
from agentTest.tests.test_schema_document_retriever import run_schema_document_retriever_tests
from agentTest.tests.test_schema_vector_retriever import run_schema_vector_retriever_tests

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

    #hive连接
    # try:
    #     from agentTest.tests.test_hive_smoke import run_hive_smoke_tests
    #
    #     passed, count = run_hive_smoke_tests()
    #     total_passed += passed
    #     total_count += count
    # except Exception as error:
    #     print("=" * 60)
    #     print("[WARN] Hive smoke test 未执行")
    #     print(f"原因: {error}")
    #     print("=" * 60)

    #hive sql安全校验
    # passed, count = run_hive_sql_guardrails_tests()
    # total_passed += passed

    #hive获取metadata测试
    # passed, count = run_hive_metadata_provider_tests()
    # total_passed += passed
    # total_count += count

    #表召回测试
    # passed, count = run_schema_context_tests()
    # total_passed += passed
    # total_count += count

    #rag documents 构建测试
    # passed, count = run_hive_schema_documents_tests()
    # total_passed += passed
    # total_count += count

    #schema document 检索测试
    # passed, count = run_schema_document_retriever_tests()
    # total_passed += passed
    # total_count += count

    # passed, count = run_agent_schema_rag_tests()
    # total_passed += passed
    # total_count += count

    # passed, count = run_planner_prompt_schema_rag_tests()
    # total_passed += passed
    # total_count += count

    # passed, count = run_metadata_cache_tests()
    # total_passed += passed
    # total_count += count

    # passed, count = run_schema_snapshot_cache_tests()
    # total_passed += passed
    # total_count += count

    passed, count = run_schema_vector_retriever_tests()
    total_passed += passed
    total_count += count


    print("=" * 60)
    print(f"全部测试完成，通过 {total_passed}/{total_count}")


if __name__ == "__main__":
    main()
