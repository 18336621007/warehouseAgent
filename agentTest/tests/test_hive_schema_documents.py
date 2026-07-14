from agentTest.db.hive_guardrails import ALLOWED_HIVE_TABLES
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.rag.schema_document_builder import SchemaDocumentBuilder
from agentTest.rag.schema_snapshot_service import SchemaSnapshotService
from agentTest.tools.schema_tool import SchemaTool


def run_hive_schema_documents_tests():
    # 执行 Hive schema document 测试
    passed_count = 0
    total_count = 1

    print("=" * 60)
    print("开始测试组: hive_schema_documents")
    print("=" * 60)

    try:
        provider = HiveMetadataProvider()
        schema_tool = SchemaTool(provider)
        document_builder = SchemaDocumentBuilder()
        snapshot_service = SchemaSnapshotService(schema_tool, document_builder)

        documents = snapshot_service.build_snapshot()

        if not documents:
            raise AssertionError("schema documents 为空")

        target_doc_id = "hive:test.agent_order_demo"
        target_document = None

        for document in documents:
            if document.get("doc_id") == target_doc_id:
                target_document = document
                break

        if not target_document:
            raise AssertionError(f"未找到目标文档: {target_doc_id}")

        print("[PASS] Hive schema documents 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Hive schema documents 测试: {error}")

    print(f"测试组 hive_schema_documents 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count