import os

from agentTest.rag.real_embedder import RealEmbedder
from agentTest.rag.simple_embedder import SimpleEmbedder


class EmbedderFactory:
    # EmbedderFactory 负责按配置创建具体的 embedder 实例。

    @staticmethod
    def create():
        embedder_type = os.getenv("EMBEDDER_TYPE", "simple").strip().lower()

        if embedder_type == "real":
            return RealEmbedder(
                model_name=os.getenv("EMBEDDER_MODEL", ""),
                endpoint=os.getenv("EMBEDDER_ENDPOINT", ""),
                api_key=os.getenv("EMBEDDER_API_KEY", ""),
            )

        return SimpleEmbedder()