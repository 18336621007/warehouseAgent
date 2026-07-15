from agentTest.rag.base_embedder import BaseEmbedder
from agentTest.rag.simple_embedder import SimpleEmbedder


class RealEmbedder(BaseEmbedder):
    # RealEmbedder 负责适配真实 embedding 服务。
    # 当前阶段先保留 fallback 机制，后续可替换为企业 HTTP 服务或外部 embedding API。

    def __init__(
        self,
        model_name: str = "",
        endpoint: str = "",
        api_key: str = "",
        timeout_seconds: int = 10,
        enable_fallback: bool = True, #
    ):
        self.model_name = model_name
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.enable_fallback = enable_fallback

        self.fallback_embedder = SimpleEmbedder()  # 远程失败时的本地降级方案
        self._cache = {}  # embedding 结果缓存
        self._request_count = 0  # 统计远程请求次数，便于测试缓存
        self._fallback_count = 0  # 统计 fallback 次数，便于观察外部依赖稳定性

    def _normalize_text(self, text: str) -> str:
        # 统一清洗输入文本，避免空值和重复缓存 key。
        return str(text or "").strip()

    def _build_headers(self) -> dict:
        # 构造远程 embedding 请求头。
        headers = {
            "Content-Type": "application/json",
        }

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        return headers

    def _build_payload(self, text: str) -> dict:
        # 构造远程 embedding 请求体。
        return {
            "model": self.model_name,
            "input": text,
        }

    def _request_embedding(self, text: str) -> dict[str, float]:
        # 发送远程 embedding 请求。
        # 当前阶段先保留接口壳，后续替换为真实 HTTP 调用。
        self._request_count += 1

        if not self.endpoint:
            raise ValueError("embedding endpoint 未配置")

        raise RuntimeError("当前 RealEmbedder 尚未接入真实远程服务")

    def embed(self, text: str) -> dict[str, float]:
        # 将输入文本转换为 embedding，支持缓存与 fallback。
        normalized_text = self._normalize_text(text)
        if not normalized_text:
            return {}

        if normalized_text in self._cache:
            return dict(self._cache[normalized_text])

        try:
            vector = self._request_embedding(normalized_text)
            self._cache[normalized_text] = dict(vector)
            return dict(vector)
        except Exception:
            if self.enable_fallback:
                self._fallback_count += 1
                vector = self.fallback_embedder.embed(normalized_text)
                self._cache[normalized_text] = dict(vector)
                return dict(vector)
            raise

    def cosine_similarity(self, left: dict[str, float], right: dict[str, float]) -> float:
        # 当前沿用本地 embedder 的相似度计算逻辑。
        return self.fallback_embedder.cosine_similarity(left, right)

    def clear_cache(self):
        # 清空 embedding 缓存。
        self._cache = {}