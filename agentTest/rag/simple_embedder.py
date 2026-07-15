import math
import re


class SimpleEmbedder:
    # SimpleEmbedder 负责把文本转换成可比较的稀疏向量。
    # 当前先用本地轻量方案跑通链路，后续可替换为真实 embedding 模型。

    def tokenize(self, text: str) -> list[str]:
        # 对输入文本做简单切词，统一转小写。
        normalized = str(text or "").lower()
        parts = re.split(r"[\s\_\-\.\,\:\;\(\)\[\]\{\}]+", normalized)
        tokens = [part for part in parts if part]
        return tokens

    def embed(self, text: str) -> dict[str, float]:
        # 将文本编码成 token -> weight 的稀疏向量。目前是简单的，即出现一次则+1，验证流程是否能跑通
        vector = {}
        for token in self.tokenize(text):
            vector[token] = vector.get(token, 0.0) + 1.0
        return vector

    def cosine_similarity(self, left: dict[str, float], right: dict[str, float]) -> float:
        # 计算两个稀疏向量的余弦相似度。
        if not left or not right:
            return 0.0

        dot_value = 0.0
        for token, weight in left.items():
            dot_value += weight * right.get(token, 0.0)

        left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
        right_norm = math.sqrt(sum(weight * weight for weight in right.values()))

        if left_norm == 0 or right_norm == 0:
            return 0.0

        return dot_value / (left_norm * right_norm)