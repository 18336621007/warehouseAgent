# 该文件用于测试 schema snapshot cache 是否生效，
# 重点验证 build_snapshot 的缓存命中、_clear_snapshot_cache 生效和 snapshot 内容正确性。
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.rag.schema_document_builder import SchemaDocumentBuilder
from agentTest.rag.schema_snapshot_service import SchemaSnapshotService
from agentTest.tools.schema_tool import SchemaTool

schema_snapshot_cache_cases = {
    "build_snapshot 缓存": {
        "case_type": "build_snapshot_cache",
    },
    "clear_snapshot_cache 生效": {
        "case_type": "clear_snapshot_cache",
    },
    "snapshot 内容正确": {
        "case_type": "snapshot_content",
        "expected_doc_id": "hive:test.agent_order_demo",
    },
}

def run_schema_snapshot_cache_tests():
    # 执行 schema snapshot cache 测试，验证 snapshot 缓存和清理逻辑是否正确
    passed_count = 0
    total_count = len(schema_snapshot_cache_cases)

    print("=" * 60)
    print("开始测试组: schema_snapshot_cache")
    print("=" * 60)

    for case_name, config in schema_snapshot_cache_cases.items():
        case_type = config["case_type"]

        print("-" * 60)
        print(f"测试样例: {case_name}")

        try:
            provider = HiveMetadataProvider()
            schema_tool = SchemaTool(provider)
            document_builder = SchemaDocumentBuilder()
            snapshot_service = SchemaSnapshotService(schema_tool, document_builder)

            if case_type == "build_snapshot_cache":
                snapshot_1 = snapshot_service.build_snapshot()
                snapshot_2 = snapshot_service.build_snapshot()

                print("第一次 build_snapshot 结果:")
                print(snapshot_1)
                print("第二次 build_snapshot 结果:")
                print(snapshot_2)
                print(f"_build_snapshot_count={snapshot_service._build_snapshot_count}")

                if not snapshot_1:
                    raise AssertionError("第一次 build_snapshot 结果为空")

                if not snapshot_2:
                    raise AssertionError("第二次 build_snapshot 结果为空")

                if snapshot_service._build_snapshot_count != 1:
                    raise AssertionError(
                        f"期望 build_snapshot 实际构建次数为 1，实际为 {snapshot_service._build_snapshot_count}"
                    )

            elif case_type == "clear_snapshot_cache":
                snapshot_service.build_snapshot()
                first_count = snapshot_service._build_snapshot_count

                snapshot_service.clear_snapshot_cache()

                snapshot_service.build_snapshot()
                second_count = snapshot_service._build_snapshot_count

                print(f"_clear_snapshot_cache 前 build_count={first_count}")
                print(f"_clear_snapshot_cache 后 build_count={second_count}")

                if first_count != 1:
                    raise AssertionError(
                        f"_clear_snapshot_cache 前 build_count 期望为 1，实际为 {first_count}"
                    )

                if second_count != 2:
                    raise AssertionError(
                        f"_clear_snapshot_cache 后 build_count 期望为 2，实际为 {second_count}"
                    )

            elif case_type == "snapshot_content":
                expected_doc_id = config["expected_doc_id"]

                snapshot = snapshot_service.build_snapshot()
                print("build_snapshot 结果:")
                print(snapshot)

                if not snapshot:
                    raise AssertionError("snapshot 结果为空")

                doc_ids = [document.get("doc_id", "") for document in snapshot]
                if expected_doc_id not in doc_ids:
                    raise AssertionError(f"snapshot 中未找到目标文档: {expected_doc_id}")

            else:
                raise AssertionError(f"未知 case_type: {case_type}")

            print(f"[PASS] {case_name}")
            passed_count += 1
        except Exception as error:
            print(f"[FAIL] {case_name}: {error}")

        print()

    print(f"测试组 schema_snapshot_cache 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count