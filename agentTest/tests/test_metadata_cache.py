# 该文件用于测试 metadata provider 的缓存能力，
# 重点验证 list_tables、describe_table 和 clear_cache 是否按预期生效。
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider


metadata_cache_cases = {
    "list_tables 缓存": {
        "case_type": "list_tables",
    },
    "describe_table 缓存": {
        "case_type": "describe_table",
        "table_name": "agent_order_demo",
    },
    "clear_cache 生效": {
        "case_type": "clear_cache",
    },
}


def run_metadata_cache_tests():
    # 执行 metadata cache 测试，验证缓存命中与清理逻辑是否正确
    passed_count = 0
    total_count = len(metadata_cache_cases)

    print("=" * 60)
    print("开始测试组: metadata_cache")
    print("=" * 60)

    for case_name, config in metadata_cache_cases.items():
        case_type = config["case_type"]

        print("-" * 60)
        print(f"测试样例: {case_name}")

        try:
            provider = HiveMetadataProvider()

            if case_type == "list_tables":
                tables_1 = provider.list_tables()
                tables_2 = provider.list_tables()

                print("第一次 list_tables 结果:")
                print(tables_1)
                print("第二次 list_tables 结果:")
                print(tables_2)
                print(f"_list_tables_query_cnt={provider._list_tables_query_cnt}")

                if not tables_1:
                    raise AssertionError("第一次 list_tables 结果为空")

                if not tables_2:
                    raise AssertionError("第二次 list_tables 结果为空")

                if provider._list_tables_query_cnt != 1:
                    raise AssertionError(
                        f"期望 list_tables 实际查询次数为 1，实际为 {provider._list_tables_query_cnt}"
                    )

            elif case_type == "describe_table":
                table_name = config["table_name"]

                schema_1 = provider.describe_table(table_name)
                schema_2 = provider.describe_table(table_name)

                print("第一次 describe_table 结果:")
                print(schema_1)
                print("第二次 describe_table 结果:")
                print(schema_2)
                print(f"_describe_table_query_cnt={provider._describe_table_query_cnt}")

                if not schema_1:
                    raise AssertionError("第一次 describe_table 结果为空")

                if not schema_2:
                    raise AssertionError("第二次 describe_table 结果为空")

                if schema_1.get("table_name") != table_name:
                    raise AssertionError(f"第一次返回表名不正确: {schema_1.get('table_name')}")

                if schema_2.get("table_name") != table_name:
                    raise AssertionError(f"第二次返回表名不正确: {schema_2.get('table_name')}")

                if provider._describe_table_query_cnt != 1:
                    raise AssertionError(
                        f"期望 describe_table 实际查询次数为 1，实际为 {provider._describe_table_query_cnt}"
                    )

            elif case_type == "clear_cache":
                provider.list_tables()
                first_count = provider._list_tables_query_cnt

                provider.clear_cache()

                provider.list_tables()
                second_count = provider._list_tables_query_cnt

                print(f"clear_cache 前 query_count={first_count}")
                print(f"clear_cache 后 query_count={second_count}")

                if first_count != 1:
                    raise AssertionError(f"clear_cache 前 query_count 期望为 1，实际为 {first_count}")

                if second_count != 2:
                    raise AssertionError(f"clear_cache 后 query_count 期望为 2，实际为 {second_count}")

            else:
                raise AssertionError(f"未知 case_type: {case_type}")

            print(f"[PASS] {case_name}")
            passed_count += 1
        except Exception as error:
            print(f"[FAIL] {case_name}: {error}")

        print()

    print(f"测试组 metadata_cache 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count