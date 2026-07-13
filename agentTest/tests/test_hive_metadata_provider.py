# 该文件用于测试 HiveMetadataProvider 的元数据返回结构是否稳定，
# 重点校验表列表结构、表结构详情结构以及字段结构是否满足后续 schema 检索、RAG 和语义层使用要求。
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider


def _assert_table_item(table_item: dict):
    # 校验单个表元数据项是否包含约定字段
    required_keys = {
        "database_name",
        "table_name",
        "table_comment",
        "table_type",
    }

    missing_keys = required_keys - set(table_item.keys())
    if missing_keys:
        raise AssertionError(f"表元数据缺少字段: {missing_keys}")


def _assert_column_item(column_item: dict):
    # 校验单个字段元数据项是否包含约定字段
    required_keys = {
        "name",
        "type",
        "comment",
        "nullable",
        "partition_key",
    }

    missing_keys = required_keys - set(column_item.keys())
    if missing_keys:
        raise AssertionError(f"字段元数据缺少字段: {missing_keys}")


def run_hive_metadata_provider_tests():
    # 执行 Hive metadata 契约测试，确保 provider 输出结构稳定可复用
    passed_count = 0
    total_count = 3

    print("=" * 60)
    print("开始测试组: hive_metadata_provider")
    print("=" * 60)

    provider = HiveMetadataProvider()

    # 1. 测试表列表结构
    try:
        tables = provider.list_tables()
        print("[INFO] list_tables 返回结果:")
        print(tables[:5] if isinstance(tables, list) else tables)

        if not isinstance(tables, list):
            raise AssertionError("list_tables 返回值必须是 list")

        if not tables:
            raise AssertionError("list_tables 返回结果为空，无法验证结构")

        _assert_table_item(tables[0])

        print("[PASS] Hive 表列表结构测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Hive 表列表结构测试: {error}")

    print()

    # 2. 测试指定测试表结构详情
    try:
        schema = provider.describe_table("agent_order_demo")
        print("[INFO] describe_table 返回结果:")
        print(schema)

        if not isinstance(schema, dict):
            raise AssertionError("describe_table 返回值必须是 dict")

        required_schema_keys = {
            "database_name",
            "table_name",
            "table_comment",
            "table_type",
            "columns",
        }

        missing_schema_keys = required_schema_keys - set(schema.keys())
        if missing_schema_keys:
            raise AssertionError(f"表结构详情缺少字段: {missing_schema_keys}")

        if not isinstance(schema["columns"], list):
            raise AssertionError("columns 字段必须是 list")

        if not schema["columns"]:
            raise AssertionError("columns 为空，无法验证字段结构")

        print("[PASS] Hive 表结构详情测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Hive 表结构详情测试: {error}")

    print()

    # 3. 测试字段结构契约
    try:
        schema = provider.describe_table("agent_order_demo")
        first_column = schema["columns"][0]
        _assert_column_item(first_column)

        print("[INFO] 首个字段结构:")
        print(first_column)

        print("[PASS] Hive 字段结构契约测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Hive 字段结构契约测试: {error}")

    print()

    print(f"测试组 hive_metadata_provider 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count