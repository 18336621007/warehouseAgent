
def run_hive_smoke_tests():
    # Hive 最小连通性与元数据冒烟测试，确保接入前置能力可用
    from agentTest.datasource.hive_datasource import HiveDataSource
    from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
    passed_count = 0
    total_count = 3

    print("=" * 60)
    print("开始测试组: hive_smoke")
    print("=" * 60)

    try:
        datasource = HiveDataSource()
        result = datasource.query("select 1 as a")
        print("[PASS] Hive 连通性测试")
        print(result)
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Hive 连通性测试: {error}")

    try:
        provider = HiveMetadataProvider()
        tables = provider.list_tables()
        print("[PASS] Hive 表列表测试")
        print(tables[:5])
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Hive 表列表测试: {error}")

    try:
        provider = HiveMetadataProvider()
        schema = provider.describe_table("agent_order_demo")
        print("[PASS] Hive 表结构测试")
        print(schema)
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Hive 表结构测试: {error}")

    print(f"测试组 hive_smoke 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count