import hashlib
import json
import os


class SchemaEmbeddingCache:
    # SchemaEmbeddingCache 负责管理 schema document 字段级 embedding 的本地持久化缓存。

    def __init__(self, cache_file: str):
        # cache_file 为本地缓存文件路径。
        self.cache_file = cache_file
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        # 从本地文件加载缓存。
        if not os.path.exists(self.cache_file):
            return {}

        with open(self.cache_file, "r", encoding="utf-8") as file:
            return json.load(file)

    def _save_cache(self):
        # 将缓存写回本地文件。
        cache_dir = os.path.dirname(self.cache_file)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        with open(self.cache_file, "w", encoding="utf-8") as file:
            json.dump(self._cache, file, ensure_ascii=False, indent=2)

    def build_document_hash(self, document: dict) -> str:
        # 根据 document 关键字段生成内容指纹，避免同表结构变化后误用旧缓存。
        payload = {
            "doc_id": document.get("doc_id", ""),
            "table_name": document.get("table_name", ""),
            "table_comment": document.get("table_comment", ""),
            "summary": document.get("summary", ""),
            "content": document.get("content", ""),
            "keywords": document.get("keywords", []),
            "columns": document.get("columns", []),
        }

        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw_text.encode("utf-8")).hexdigest()

    def build_field_cache_key(self, document: dict, field_name: str) -> str:
        # 构造字段级缓存 key，区分同一 document 的不同字段向量。
        doc_id = document.get("doc_id", "")
        doc_hash = self.build_document_hash(document)
        return f"{doc_id}:{doc_hash}:{field_name}"

    def get_field(self, document: dict, field_name: str):
        # 获取某个 document 指定字段的 embedding 缓存。
        cache_key = self.build_field_cache_key(document, field_name)
        vector = self._cache.get(cache_key)

        if vector is None:
            return None

        return dict(vector)

    def set_field(self, document: dict, field_name: str, vector: dict[str, float]):
        # 写入某个 document 指定字段的 embedding 缓存，并立即持久化。
        cache_key = self.build_field_cache_key(document, field_name)
        self._cache[cache_key] = dict(vector)
        self._save_cache()

    def clear(self):
        # 清空内存缓存和本地缓存文件内容。
        self._cache = {}
        self._save_cache()