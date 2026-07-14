# 该文件用于测试 Agent 是否在主链路前置接入了 schema RAG，
# 测试重点是 _build_schema_rag_context 是否能返回预期的目标文档。
import os

from agentTest.agent import Agent


def run_agent_schema_rag_tests():
    passed_count = 0
    total_count = 1

    print("=" * 60)
    print("开始测试组: agent_schema_rag")
    print("=" * 60)

    try:
        # 测试前显式指定 Hive 数据源，避免默认 mysql 分支影响结果
        os.environ["DATA_SOURCE_TYPE"] = "hive"

        agent = Agent()
        context = agent._build_schema_rag_context("查订单金额")

        print("SchemaRAGContext:")
        print(context)

        if not context:
            raise AssertionError("schema_rag_context 为空")

        top_doc_id = context[0].get("doc_id", "")
        if top_doc_id != "hive:test.agent_order_demo":
            raise AssertionError(f"预期第一名文档为 hive:test.agent_order_demo，实际为 {top_doc_id}")

        print("[PASS] Agent schema RAG 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] Agent schema RAG 测试: {error}")

    print(f"测试组 agent_schema_rag 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
