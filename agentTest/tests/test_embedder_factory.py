import os

from agentTest.rag.embedder_factory import EmbedderFactory
from agentTest.rag.real_embedder import RealEmbedder
from agentTest.rag.simple_embedder import SimpleEmbedder


def run_embedder_factory_tests():
    # 该测试文件用于验证 embedder 工厂是否能按配置返回正确实现。
    passed_count = 0
    total_count = 2

    print("=" * 60)
    print("开始测试组: embedder_factory")
    print("=" * 60)

    try:
        os.environ.pop("EMBEDDER_TYPE", None)
        embedder = EmbedderFactory.create()

        if not isinstance(embedder, SimpleEmbedder):
            raise AssertionError(f"默认 embedder 应为 SimpleEmbedder，实际为 {type(embedder)}")

        print("[PASS] 默认 embedder 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] 默认 embedder 测试: {error}")

    try:
        os.environ["EMBEDDER_TYPE"] = "real"
        embedder = EmbedderFactory.create()

        if not isinstance(embedder, RealEmbedder):
            raise AssertionError(f"real 模式应返回 RealEmbedder，实际为 {type(embedder)}")

        print("[PASS] real embedder 测试")
        passed_count += 1
    except Exception as error:
        print(f"[FAIL] real embedder 测试: {error}")

    print(f"测试组 embedder_factory 完成，通过 {passed_count}/{total_count}")
    print()
    return passed_count, total_count