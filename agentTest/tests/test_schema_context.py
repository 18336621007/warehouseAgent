# 该文件用于测试基于真实 Hive metadata 构建 schema_context 的结果是否稳定，
# 重点验证候选表、候选字段是否包含预期业务语义字段，便于后续接入 RAG。
from agentTest.db.hive_guardrails import ALLOWED_HIVE_TABLES
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.schema.schema_context_builder import SchemaContextBuilder
from agentTest.tools.schema_tool import SchemaTool


schema_context_cases = {
    "查订单": {
        "query": "查订单",
        "expected_table": "agent_order_demo",
        "expected_column": "order_id",
    },
    "查订单金额": {
        "query": "查订单金额",
        "expected_table": "agent_order_demo",
        "expected_column": "pay_amt",
    },
    "查支付时间": {
        "query": "查支付时间",
        "expected_table": "agent_order_demo",
        "expected_column": "pay_time",
    },
    "查城市订单": {
        "query": "查城市订单",
        "expected_table": "agent_order_demo",
        "expected_column": "city",
    },
    "查订单状态": {
        "query": "查订单状态",
        "expected_table": "agent_order_demo",
        "expected_column": "order_status",
    },
}


def run_schema_context_tests():
    # 执行 schema_context 构建测试，验证真实 Hive metadata 下的召回效果
    passed_count = 0
    total_count = len(schema_context_cases)

    print("=" * 60)
    print("开始测试组: schema_context")
    print("=" * 60)

    provider = HiveMetadataProvider()
    schema_tool = SchemaTool(provider)
    builder = SchemaContextBuilder(schema_tool)

    for case_name, config in schema_context_cases.items():
        query = config["query"]
        expected_table = config["expected_table"]
        expected_column = config["expected_column"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"Query: {query}")

        try:
            schema_context = builder.build(query)
            print("SchemaContext:")
            print(schema_context)

            candidate_tables = schema_context.get("candidate_tables", [])
            if not candidate_tables:
                raise AssertionError("candidate_tables 为空")

            table_names = [table.get("table_name", "") for table in candidate_tables]
            if expected_table not in table_names:
                raise AssertionError(f"未召回预期表: {expected_table}")

            if ALLOWED_HIVE_TABLES:
                invalid_tables = [table_name for table_name in table_names if table_name not in ALLOWED_HIVE_TABLES]
                if invalid_tables:
                    raise AssertionError(f"召回了白名单外表: {invalid_tables}")

            target_table = next(table for table in candidate_tables if table.get("table_name") == expected_table)
            column_names = [column.get("name", "") for column in target_table.get("candidate_columns", [])]
            if expected_column not in column_names:
                raise AssertionError(f"未召回预期字段: {expected_column}")

            print(f"[PASS] {case_name}")
            passed_count += 1
        except Exception as error:
            print(f"[FAIL] {case_name}: {error}")

        print()

    print(f"测试组 schema_context 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
