from agentTest.rag.base_embedder import BaseEmbedder
from agentTest.rag.simple_embedder import SimpleEmbedder


class RealEmbedder(BaseEmbedder):
    # RealEmbedder 代表真实 embedding 服务的适配层。
    # 当前阶段先复用 SimpleEmbedder，后续再替换为真实模型或远程服务。

    def __init__(self, model_name: str = "", endpoint: str = "", api_key: str = ""):
        self.model_name = model_name
        self.endpoint = endpoint
        self.api_key = api_key
        self._fallback_embedder = SimpleEmbedder()

    def embed(self, text: str) -> dict[str, float]:
        # 当前先复用本地 embedder，后续替换为真实服务调用。
        return self._fallback_embedder.embed(text)

    def cosine_similarity(self, left: dict[str, float], right: dict[str, float]) -> float:
        # 当前仍沿用本地相似度计算逻辑。
        return self._fallback_embedder.cosine_similarity(left, right)