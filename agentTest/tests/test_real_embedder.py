from agentTest.rag.real_embedder import RealEmbedder


def run_real_embedder_tests():
    # 该测试文件用于验证 RealEmbedder 的缓存、fallback 和空输入处理逻辑。
    passed_count = 0
    total_count = 4

    print("=" * 60)
    print("开始测试组: real_embedder")
    print("=" * 60)

    # 测试 case 1：未配置 endpoint 时，若开启 fallback，应返回本地向量。
    try:
        embedder = RealEmbedder(
            model_name="demo-model",
            endpoint="",
            api_key="",
            timeout_seconds=5,
            enable_fallback=True,
        )

        vector = embedder.embed("查订单")

        if not isinstance(vector, dict):
            raise AssertionError("fallback 后返回值不是 dict")

        if not vector:
            raise AssertionError("fallback 后返回空向量")

        if embedder._fallback_count <= 0:
            raise AssertionError("未记录 fallback 次数")

        print("[PASS] endpoint 缺失 fallback 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] endpoint 缺失 fallback 测试: {error}")

    # 测试 case 2：空文本应直接返回空向量。
    try:
        embedder = RealEmbedder(
            model_name="demo-model",
            endpoint="",
            api_key="",
            timeout_seconds=5,
            enable_fallback=True,
        )

        vector = embedder.embed("")

        if vector != {}:
            raise AssertionError(f"空文本应返回空字典，实际为 {vector}")

        print("[PASS] 空文本测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 空文本测试: {error}")

    # 测试 case 3：相同文本二次请求应命中缓存。
    try:
        embedder = RealEmbedder(
            model_name="demo-model",
            endpoint="",
            api_key="",
            timeout_seconds=5,
            enable_fallback=True,
        )

        vector_1 = embedder.embed("查订单")
        fallback_count_1 = embedder._fallback_count

        vector_2 = embedder.embed("查订单")
        fallback_count_2 = embedder._fallback_count

        if vector_1 != vector_2:
            raise AssertionError("相同文本两次 embedding 结果不一致")

        if fallback_count_2 != fallback_count_1:
            raise AssertionError("第二次相同文本请求未命中缓存，fallback 次数异常增加")

        print("[PASS] embedding 缓存测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] embedding 缓存测试: {error}")

    # 测试 case 4：关闭 fallback 时，应向上抛出异常。
    try:
        embedder = RealEmbedder(
            model_name="demo-model",
            endpoint="",
            api_key="",
            timeout_seconds=5,
            enable_fallback=False,
        )

        try:
            embedder.embed("查订单")
            raise AssertionError("关闭 fallback 后预期应抛出异常，但未抛出")
        except ValueError:
            pass

        print("[PASS] fallback 关闭异常测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] fallback 关闭异常测试: {error}")

    print(f"测试组 real_embedder 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count