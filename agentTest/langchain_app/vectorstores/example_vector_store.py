# 高质量对话示例向量库 —— 存储 Evaluator >= 80 分的对话
# v2: 新增 hash_id 去重、remove_example 删除、sync_by_score 同步
import os, json, hashlib
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_community.vectorstores.utils import DistanceStrategy

_CACHE_EXAMPLE_DIR = "cache/example_faiss_index"


class ExampleVectorStore:
    """管理高质量对话示例的 FAISS 向量库，支持增量写入、去重和结构感知检索"""

    def __init__(self, embeddings):
        self.embeddings = embeddings
        self._vector_store = None

    def _hash_id(self, question: str, sql: str) -> str:
        """基于问题+SQL 的 MD5 生成唯一标识，用于精确去重"""
        raw = question + sql
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def _ensure_loaded(self):
        """懒加载：优先从磁盘加载，不存在则创建空库"""
        if self._vector_store is not None:
            return
        path = _CACHE_EXAMPLE_DIR
        if os.path.exists(path) and os.listdir(path):
            try:
                self._vector_store = FAISS.load_local(
                    path, self.embeddings, allow_dangerous_deserialization=True
                )
                return
            except Exception:
                pass
        placeholder = Document(page_content="placeholder", metadata={"_placeholder": True})
        self._vector_store = FAISS.from_documents(
            [placeholder], self.embeddings, distance_strategy=DistanceStrategy.COSINE
        )

    def add_example(self, question: str, sql: str, answer: str,
                    tables: list, fields: list, domain_tag: str, score: float):
        """新增高质量示例。先去重（hash + 语义），再写入并落盘。"""
        self._ensure_loaded()
        hash_id = self._hash_id(question, sql)

        # 去重1: 精确 hash 匹配
        for doc_id, doc in self._vector_store.docstore._dict.items():
            if doc.metadata.get("hash_id") == hash_id:
                old_score = doc.metadata.get("score", 0)
                doc.metadata["score"] = max(old_score, score)
                self._save_to_disk()
                return

        # 去重2: 语义相似度匹配（cosine > 0.95 视为重复）
        existing = self._vector_store.similarity_search_with_score(question, k=1)
        if existing:
            top_doc, top_score = existing[0]
            if not top_doc.metadata.get("_placeholder"):
                similarity = 1 - top_score / 2
                if similarity > 0.95:
                    old_score = top_doc.metadata.get("score", 0)
                    top_doc.metadata["score"] = max(old_score, score)
                    top_doc.metadata["hash_id"] = hash_id
                    self._save_to_disk()
                    return

        # 写入新记录
        page_content = f"问题: {question}\nSQL: {sql}\n答案: {answer[:500]}"
        metadata = {
            "question": question,
            "sql": sql,
            "tables": json.dumps(tables, ensure_ascii=False),
            "fields": json.dumps(fields, ensure_ascii=False),
            "domain": domain_tag,
            "score": score,
            "hash_id": hash_id,
            "_placeholder": False,
        }
        doc = Document(page_content=page_content, metadata=metadata)
        self._vector_store.add_documents([doc])
        self._save_to_disk()

    def remove_example(self, hash_id: str):
        """删除指定 hash_id 的示例并落盘"""
        self._ensure_loaded()
        ids_to_delete = []
        for doc_id, doc in self._vector_store.docstore._dict.items():
            if doc.metadata.get("hash_id") == hash_id:
                ids_to_delete.append(doc_id)
        if ids_to_delete:
            self._vector_store.delete(ids_to_delete)
            self._save_to_disk()

    def sync_by_score(self, hash_id: str, question: str, sql: str, answer: str,
                      tables: list, fields: list, domain_tag: str,
                      score: float, is_high: bool):
        """根据评分结果同步 FAISS：高分则加，低分则删"""
        if is_high:
            self.add_example(question, sql, answer, tables, fields, domain_tag, score)
        else:
            self.remove_example(hash_id)

    def search_similar(self, question: str, current_tables: list = None, k: int = 3) -> list:
        """结构感知检索：语义相似 -> 按表/字段重叠度重排序"""
        self._ensure_loaded()
        if self._vector_store is None:
            return []
        docs_with_scores = self._vector_store.similarity_search_with_score(question, k=k * 3)
        candidates = [(doc, score) for doc, score in docs_with_scores
                      if not doc.metadata.get("_placeholder")]
        if not candidates:
            return []
        if current_tables:
            scored = []
            for doc, score in candidates:
                doc_tables = json.loads(doc.metadata.get("tables", "[]"))
                doc_fields = json.loads(doc.metadata.get("fields", "[]"))
                table_overlap = len(set(doc_tables) & set(current_tables)) / max(len(current_tables), 1)
                field_overlap = len(set(doc_fields)) / max(len(doc_fields), 1) if doc_fields else 0
                structure_score = (table_overlap + field_overlap) / 2
                combined = 0.3 * (1 - score / 2) + 0.7 * structure_score
                scored.append((doc, combined))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [doc for doc, _ in scored[:k]]
        candidates.sort(key=lambda x: x[1])
        return [doc for doc, _ in candidates[:k]]

    def _save_to_disk(self):
        """落盘到 cache/example_faiss_index"""
        os.makedirs(_CACHE_EXAMPLE_DIR, exist_ok=True)
        if self._vector_store is not None:
            self._vector_store.save_local(_CACHE_EXAMPLE_DIR)
