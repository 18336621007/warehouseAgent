# 该文件用于测试 schema document 检索器是否能稳定命中正确表文档，
# 这里使用 mock documents，避免依赖真实 Hive 环境导致测试不稳定。
from agentTest.rag.schema_document_retriever import SchemaDocumentRetriever


schema_document_retriever_cases = {
    "查订单": {
        "query": "查订单",
        "expected_doc_id": "hive:test.agent_order_demo",
    },
    "查订单金额": {
        "query": "查订单金额",
        "expected_doc_id": "hive:test.agent_order_demo",
    },
    "查支付时间": {
        "query": "查支付时间",
        "expected_doc_id": "hive:test.agent_order_demo",
    },
    "查用户城市": {
        "query": "查用户城市",
        "expected_doc_id": "hive:test.agent_user_demo",
    },
}


def build_mock_documents():
    # 构造稳定的 mock schema documents，专门用于测试检索逻辑
    return [
        {
            "doc_id": "hive:test.agent_order_demo",
            "source": "hive_metadata",
            "database_name": "test",
            "table_name": "agent_order_demo",
            "summary": "test.agent_order_demo，包含字段：order_id、pay_amt、pay_time、city、order_status",
            "content": "数据库: test\n表名: agent_order_demo\n字段列表:\n- order_id string\n- pay_amt decimal(10,2)\n- pay_time string\n- city string\n- order_status string",
            "keywords": ["test", "agent_order_demo", "order_id", "pay_amt", "pay_time", "city", "order_status"],
        },
        {
            "doc_id": "hive:test.agent_user_demo",
            "source": "hive_metadata",
            "database_name": "test",
            "table_name": "agent_user_demo",
            "summary": "test.agent_user_demo，包含字段：user_id、user_name、city",
            "content": "数据库: test\n表名: agent_user_demo\n字段列表:\n- user_id string\n- user_name string\n- city string",
            "keywords": ["test", "agent_user_demo", "user_id", "user_name", "city"],
        }
    ]


def run_schema_document_retriever_tests():
    # 执行 schema document 检索测试，验证 query 到文档的召回结果是否正确
    passed_count = 0
    total_count = len(schema_document_retriever_cases)

    print("=" * 60)
    print("开始测试组: schema_document_retriever")
    print("=" * 60)

    retriever = SchemaDocumentRetriever()
    documents = build_mock_documents()

    for case_name, config in schema_document_retriever_cases.items():
        query = config["query"]
        expected_doc_id = config["expected_doc_id"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"Query: {query}")

        try:
            results = retriever.retrieve(query, documents, top_k=2)
            print("RetrieveResults:")
            print(results)

            if not results:
                raise AssertionError("检索结果为空")

            top_doc_id = results[0].get("doc_id", "")
            if top_doc_id != expected_doc_id:
                raise AssertionError(f"预期第一名文档为 {expected_doc_id}，实际为 {top_doc_id}")

            print(f"[PASS] {case_name}")
            passed_count += 1
        except Exception as error:
            print(f"[FAIL] {case_name}: {error}")

        print()

    print(f"测试组 schema_document_retriever 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
