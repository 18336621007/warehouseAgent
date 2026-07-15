import math
import re


class SimpleEmbedder:
    # SimpleEmbedder 负责文本切词、业务词扩展和稀疏向量构建。
    # 当前先使用本地轻量方案跑通链路，后续可替换为真实 embedding 模型。

    TERM_ALIAS_MAP = {
        "订单": ["order", "orders", "order_id", "order_status"],
        "金额": ["amount", "amt", "pay_amt", "actual_amt", "gmv"],
        "支付": ["pay", "payment", "pay_time", "pay_amt"],
        "时间": ["time", "date", "dt", "create_time", "pay_time"],
        "用户": ["user", "user_id", "buyer_id"],
        "状态": ["status", "state", "order_status"],
        "商品": ["item", "sku", "product", "product_id"],
    }

    def basic_tokenize(self, text: str) -> list[str]:
        # 只负责基础切词，不做业务语义扩展。
        normalized = str(text or "").lower()
        parts = re.split(r"[\s\_\-\.\,\:\;\(\)\[\]\{\}]+", normalized)
        return [part for part in parts if part]

    def expand_tokens(self, text: str, tokens: list[str]) -> list[str]:
        # 根据中文业务词补充英文别名，增强 query 与 schema 的桥接能力。
        normalized = str(text or "").lower()
        expanded_tokens = list(tokens)

        for term, aliases in self.TERM_ALIAS_MAP.items():
            if term in normalized:
                expanded_tokens.append(term)
                expanded_tokens.extend(aliases)

        return expanded_tokens

    def tokenize(self, text: str) -> list[str]:
        # 对外统一切词入口：先基础切词，再做业务词扩展。
        tokens = self.basic_tokenize(text)
        return self.expand_tokens(text, tokens)

    def embed_tokens(self, tokens: list[str]) -> dict[str, float]:
        # 将 token 列表编码成稀疏向量。
        vector = {}
        for token in tokens:
            vector[token] = vector.get(token, 0.0) + 1.0
        return vector

    def embed(self, text: str) -> dict[str, float]:
        # 将输入文本转换为稀疏向量。
        tokens = self.tokenize(text)
        return self.embed_tokens(tokens)

    def cosine_similarity(self, left: dict[str, float], right: dict[str, float]) -> float:
        # 计算两个稀疏向量的余弦相似度。
        if not left or not right:
            return 0.0

        dot_value = 0.0
        for token, weight in left.items():
            dot_value += weight * right.get(token, 0.0)

        left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
        right_norm = math.sqrt(sum(weight * weight for weight in right.values()))

        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0

        return dot_value / (left_norm * right_norm)
