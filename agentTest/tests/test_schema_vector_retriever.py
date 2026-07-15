import os

from agentTest.rag.schema_document_builder import SchemaDocumentBuilder
from agentTest.rag.schema_snapshot_service import SchemaSnapshotService
from agentTest.rag.simple_embedder import SimpleEmbedder
from agentTest.rag.schema_vector_index import SchemaVectorIndex
from agentTest.rag.schema_vector_retriever import SchemaVectorRetriever
from agentTest.tools.schema_tool import SchemaTool
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider


def run_schema_vector_retriever_tests():
    # 该测试文件用于验证第二课新增的向量版 schema RAG 是否工作正常。
    passed_count = 0
    total_count = 3

    print("=" * 60)
    print("开始测试组: schema_vector_retriever")
    print("=" * 60)

    try:
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        provider = HiveMetadataProvider()
        schema_tool = SchemaTool(provider)
        document_builder = SchemaDocumentBuilder()
        snapshot_service = SchemaSnapshotService(schema_tool, document_builder)
        embedder = SimpleEmbedder()
        vector_index = SchemaVectorIndex(embedder)
        retriever = SchemaVectorRetriever(embedder, vector_index)

        documents = snapshot_service.build_snapshot()
        result = retriever.retrieve("查订单", documents, top_k=3)

        if not result:
            raise AssertionError("查询“查订单”未召回任何文档")

        if result[0].get("doc_id") != "hive:test.agent_order_demo":
            raise AssertionError(f"预期 top1 为 hive:test.agent_order_demo，实际为 {result[0].get('doc_id')}")

        print("[PASS] 向量召回订单表测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 向量召回订单表测试: {error}")

    try:
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        provider = HiveMetadataProvider()
        schema_tool = SchemaTool(provider)
        document_builder = SchemaDocumentBuilder()
        snapshot_service = SchemaSnapshotService(schema_tool, document_builder)
        embedder = SimpleEmbedder()
        vector_index = SchemaVectorIndex(embedder)
        retriever = SchemaVectorRetriever(embedder, vector_index)

        documents = snapshot_service.build_snapshot()
        result = retriever.retrieve("查订单金额", documents, top_k=3)

        if not result:
            raise AssertionError("查询“查订单金额”未召回任何文档")

        doc_ids = [item.get("doc_id", "") for item in result]
        if "hive:test.agent_order_demo" not in doc_ids:
            raise AssertionError(f"结果中未包含目标表，实际结果为 {doc_ids}")

        print("[PASS] 向量召回金额相关表测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 向量召回金额相关表测试: {error}")

    try:
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        provider = HiveMetadataProvider()
        schema_tool = SchemaTool(provider)
        document_builder = SchemaDocumentBuilder()
        snapshot_service = SchemaSnapshotService(schema_tool, document_builder)
        embedder = SimpleEmbedder()
        vector_index = SchemaVectorIndex(embedder)
        retriever = SchemaVectorRetriever(embedder, vector_index)

        documents = snapshot_service.build_snapshot()
        result = retriever.retrieve("查订单", documents, top_k=2)

        if len(result) > 2:
            raise AssertionError(f"top_k=2 时返回数量异常，实际为 {len(result)}")

        if result and "score" not in result[0]:
            raise AssertionError("召回结果缺少 score 字段")

        print("[PASS] 向量召回 top_k 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 向量召回 top_k 测试: {error}")

    print(f"测试组 schema_vector_retriever 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count