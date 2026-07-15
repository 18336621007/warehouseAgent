import os

from agentTest.rag.schema_embedding_cache import SchemaEmbeddingCache


def _build_demo_document():
    # 构造测试用的 schema document。
    return {
        "doc_id": "hive:test.agent_order_demo",
        "table_name": "agent_order_demo",
        "table_comment": "订单测试表",
        "summary": "该表用于测试订单查询场景",
        "content": "包含订单号、用户ID、订单金额、支付时间等字段",
        "keywords": ["order", "amount", "pay_time"],
        "columns": [
            {"column_name": "order_id", "column_type": "string", "column_comment": "订单ID"},
            {"column_name": "user_id", "column_type": "string", "column_comment": "用户ID"},
            {"column_name": "pay_amt", "column_type": "decimal(10,2)", "column_comment": "支付金额"},
        ],
    }


def run_schema_embedding_cache_tests():
    # 该测试文件用于验证 schema embedding 持久化缓存的读写与 key 生成逻辑。
    passed_count = 0
    total_count = 4

    print("=" * 60)
    print("开始测试组: schema_embedding_cache")
    print("=" * 60)

    cache_file = "D:\\code\\Project\\test\\agentTest\\cache\\test_schema_embedding_cache.json"

    # 为避免上次测试残留影响本次结果，先清理旧测试文件。
    if os.path.exists(cache_file):
        os.remove(cache_file)

    # 测试 case 1：写入字段缓存后，应能正确读回。
    try:
        cache = SchemaEmbeddingCache(cache_file)
        document = _build_demo_document()
        vector = {"order": 1.0, "amount": 2.0}

        cache.set_field(document, "summary", vector)
        cached_vector = cache.get_field(document, "summary")

        if cached_vector != vector:
            raise AssertionError(f"缓存读回结果不正确，实际为 {cached_vector}")

        print("[PASS] 字段缓存读写测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 字段缓存读写测试: {error}")

    # 测试 case 2：相同 document + 相同 field 的缓存 key 应稳定一致。
    try:
        cache = SchemaEmbeddingCache(cache_file)
        document = _build_demo_document()

        key_1 = cache.build_field_cache_key(document, "summary")
        key_2 = cache.build_field_cache_key(document, "summary")

        if key_1 != key_2:
            raise AssertionError(f"相同 document 的字段缓存 key 不一致: {key_1} != {key_2}")

        print("[PASS] 字段缓存 key 稳定性测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 字段缓存 key 稳定性测试: {error}")

    # 测试 case 3：document 内容变化后，缓存 key 应发生变化。
    try:
        cache = SchemaEmbeddingCache(cache_file)
        document_1 = _build_demo_document()
        document_2 = _build_demo_document()
        document_2["summary"] = "该表用于测试订单金额统计场景"

        key_1 = cache.build_field_cache_key(document_1, "summary")
        key_2 = cache.build_field_cache_key(document_2, "summary")

        if key_1 == key_2:
            raise AssertionError("document 内容变化后，字段缓存 key 不应相同")

        print("[PASS] document 变更触发新 key 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] document 变更触发新 key 测试: {error}")

    # 测试 case 4：clear 后应清空缓存内容。
    try:
        cache = SchemaEmbeddingCache(cache_file)
        document = _build_demo_document()
        vector = {"pay": 1.0, "time": 1.0}

        cache.set_field(document, "content", vector)
        cache.clear()
        cached_vector = cache.get_field(document, "content")

        if cached_vector is not None:
            raise AssertionError(f"clear 后缓存应为空，实际为 {cached_vector}")

        print("[PASS] clear 清空缓存测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] clear 清空缓存测试: {error}")

    print(f"测试组 schema_embedding_cache 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count