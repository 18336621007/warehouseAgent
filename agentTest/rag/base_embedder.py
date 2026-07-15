class BaseEmbedder:
    # BaseEmbedder 定义 embedding 组件的统一接口。

    def embed(self, text: str) -> dict[str, float]:
        # 将输入文本编码为向量。
        raise NotImplementedError()


    def cosine_similarity(self, left: dict[str, float], right: dict[str, float]) -> float:
        # 计算两个向量的相似度。
        raise NotImplementedError()