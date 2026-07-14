# 该文件用于测试 planner prompt 是否正确注入 schema RAG 上下文，
# 重点验证 PromptBuilder 生成的提示词中是否包含目标文档的关键信息。
from agentTest.config.tools import TOOLS
from agentTest.prompt.prompt_builder import PromptBuilder


planner_prompt_schema_rag_cases = {
    "查订单金额": {
        "query": "查订单金额",
        "schema_context": {
            "candidate_tables": [
                {
                    "table_name": "agent_order_demo",
                    "candidate_columns": [
                        {"name": "order_id"},
                        {"name": "pay_amt"},
                        {"name": "pay_time"},
                    ]
                }
            ]
        },
        "schema_rag_context": [
            {
                "doc_id": "hive:test.agent_order_demo",
                "table_name": "agent_order_demo",
                "summary": "test.agent_order_demo，包含字段：order_id、pay_amt、pay_time、city、order_status",
                "keywords": ["order_id", "pay_amt", "pay_time", "city", "order_status"],
            }
        ],
        "expected_texts": [
            "Schema RAG",
            "hive:test.agent_order_demo",
            "agent_order_demo",
            "pay_amt",
        ],
    }
}


def flatten_messages(messages):
    # 把 PromptBuilder 返回的 messages 统一拼成文本，便于做断言
    if isinstance(messages, str):
        return messages

    if not isinstance(messages, list):
        return str(messages)

    parts = []
    for message in messages:
        content = message.get("content", "")
        if content:
            parts.append(content)

    return "\n".join(parts)


def run_planner_prompt_schema_rag_tests():
    # 执行 planner prompt 的 schema RAG 注入测试
    passed_count = 0
    total_count = len(planner_prompt_schema_rag_cases)

    print("=" * 60)
    print("开始测试组: planner_prompt_schema_rag")
    print("=" * 60)

    prompt_builder = PromptBuilder()

    for case_name, config in planner_prompt_schema_rag_cases.items():
        query = config["query"]
        schema_context = config["schema_context"]
        schema_rag_context = config["schema_rag_context"]
        expected_texts = config["expected_texts"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"Query: {query}")

        try:
            messages = prompt_builder.build_planner_prompt(
                query=query,
                tools=TOOLS,
                schema_context=schema_context,
                schema_rag_context=schema_rag_context,
            )

            prompt_text = flatten_messages(messages)

            print("PlannerPrompt:")
            print(prompt_text)

            if not prompt_text:
                raise AssertionError("planner prompt 为空")

            for expected_text in expected_texts:
                if expected_text not in prompt_text:
                    raise AssertionError(f"prompt 缺少预期内容: {expected_text}")

            print(f"[PASS] {case_name}")
            passed_count += 1
        except Exception as error:
            print(f"[FAIL] {case_name}: {error}")

        print()

    print(f"测试组 planner_prompt_schema_rag 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count
