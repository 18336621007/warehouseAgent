import os

from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.rag.schema_document_builder import SchemaDocumentBuilder
from agentTest.rag.schema_snapshot_service import SchemaSnapshotService
from agentTest.rag.schema_vector_index import SchemaVectorIndex
from agentTest.rag.schema_vector_retriever import SchemaVectorRetriever
from agentTest.rag.simple_embedder import SimpleEmbedder
from agentTest.tools.schema_tool import SchemaTool


def _build_schema_vector_retriever():
    # 构造测试所需的 schema 向量检索链路。
    provider = HiveMetadataProvider()
    schema_tool = SchemaTool(provider)
    document_builder = SchemaDocumentBuilder()
    snapshot_service = SchemaSnapshotService(schema_tool, document_builder)
    embedder = SimpleEmbedder()
    vector_index = SchemaVectorIndex(embedder)
    retriever = SchemaVectorRetriever(embedder, vector_index)
    return snapshot_service, retriever


def run_schema_vector_retriever_tests():
    # 该测试文件用于验证增强版 schema 向量检索是否具备基础召回与排序能力。
    passed_count = 0
    total_count = 4

    print("=" * 60)
    print("开始测试组: schema_vector_retriever")
    print("=" * 60)

    # 测试 case 1：查询“查订单”时，应能召回订单相关表。
    try:
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        snapshot_service, retriever = _build_schema_vector_retriever()
        documents = snapshot_service.build_snapshot()
        result = retriever.retrieve("查订单", documents, top_k=3)

        if not result:
            raise AssertionError("查询“查订单”未召回任何文档")

        doc_ids = [item.get("doc_id", "") for item in result]
        if "hive:test.agent_order_demo" not in doc_ids:
            raise AssertionError(f"结果中未包含目标表，实际结果为 {doc_ids}")

        print("[PASS] 向量召回订单表测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 向量召回订单表测试: {error}")

    # 测试 case 2：查询“查订单金额”时，应能召回金额相关订单表，并带有分数字段。
    try:
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        snapshot_service, retriever = _build_schema_vector_retriever()
        documents = snapshot_service.build_snapshot()
        result = retriever.retrieve("查订单金额", documents, top_k=3)

        if not result:
            raise AssertionError("查询“查订单金额”未召回任何文档")

        doc_ids = [item.get("doc_id", "") for item in result]
        if "hive:test.agent_order_demo" not in doc_ids:
            raise AssertionError(f"金额查询结果中未包含目标表，实际结果为 {doc_ids}")

        first_row = result[0]
        if "base_score" not in first_row:
            raise AssertionError("召回结果缺少 base_score 字段")

        if "bonus_score" not in first_row:
            raise AssertionError("召回结果缺少 bonus_score 字段")

        if "score" not in first_row:
            raise AssertionError("召回结果缺少 score 字段")

        if first_row["score"] < first_row["base_score"]:
            raise AssertionError("最终得分不应低于基础得分")

        print("[PASS] 向量召回金额相关表测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 向量召回金额相关表测试: {error}")

    # 测试 case 3：top_k 参数应生效，返回数量不能超过指定值。
    try:
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        snapshot_service, retriever = _build_schema_vector_retriever()
        documents = snapshot_service.build_snapshot()
        result = retriever.retrieve("查订单", documents, top_k=2)

        if len(result) > 2:
            raise AssertionError(f"top_k=2 时返回数量异常，实际为 {len(result)}")

        print("[PASS] 向量召回 top_k 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 向量召回 top_k 测试: {error}")

    # 测试 case 4：支付类查询应返回带分数的结果，便于后续观察 rerank 效果。
    try:
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        snapshot_service, retriever = _build_schema_vector_retriever()
        documents = snapshot_service.build_snapshot()
        result = retriever.retrieve("查支付时间", documents, top_k=3)

        if not result:
            raise AssertionError("查询“查支付时间”未召回任何文档")

        for item in result:
            if "score" not in item:
                raise AssertionError("支付类查询结果缺少 score 字段")

        print("[PASS] 向量召回支付语义测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 向量召回支付语义测试: {error}")

    print(f"测试组 schema_vector_retriever 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
