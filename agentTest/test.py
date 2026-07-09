from agentTest.datasource.mysql_datasource import MySQLDataSource
from agentTest.metadata.mock_metadata_provider import MockMetadataProvider
from agentTest.metadata.mysql_metadata_provider import MySQLMetadataProvider
from agentTest.schema.schema_context_builder import SchemaContextBuilder
from agentTest.tests.test_planner_validation import run_planner_validation_tests
from agentTest.tests.test_tool_validation import run_tool_validation_tests
from agentTest.tools.schema_tool import SchemaTool


def main():
    total_passed = 0
    total_count = 0

    passed, count = run_planner_validation_tests()
    total_passed += passed
    total_count += count

    passed, count = run_tool_validation_tests()
    total_passed += passed
    total_count += count

    print("=" * 60)
    print(f"全部测试完成，通过 {total_passed}/{total_count}")




if __name__ == "__main__":
    main()
