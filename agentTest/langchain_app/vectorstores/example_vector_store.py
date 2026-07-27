# 高质量对话示例向量库 —— 存储 Evaluator ≥ 80 分的对话
# 设计原因：示例需要结构感知的检索（同表同字段优先），而不仅是语义相似。
# page_content 用于 FAISS 语义检索，metadata 用于二阶段结构重排序。
import os
import json
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_community.vectorstores.utils import DistanceStrategy


_CACHE_EXAMPLE_DIR = "cache/example_faiss_index"


class ExampleVectorStore:
    """管理高质量对话示例的 FAISS 向量库，支持增量写入和结构感知检索"""

    def __init__(self, embeddings):
        self.embeddings = embeddings
        self._vector_store = None

    def _ensure_loaded(self):
        """懒加载：优先从磁盘加载，不存在则创建空库"""
        if self._vector_store is not None:
            return

        vector_store_path = _CACHE_EXAMPLE_DIR
        if os.path.exists(vector_store_path) and os.listdir(vector_store_path):
            try:
                self._vector_store = FAISS.load_local(
                    vector_store_path, self.embeddings, allow_dangerous_deserialization=True
                )
                return
            except Exception:
                pass

        # 空库：用一个占位 Document 初始化
        placeholder = Document(
            page_content="占位文档",
            metadata={"_placeholder": True}
        )
        self._vector_store = FAISS.from_documents(
            [placeholder], self.embeddings, distance_strategy=DistanceStrategy.COSINE
        )

    def add_example(self, question: str, sql: str, answer: str,
                    tables: list, fields: list, domain_tag: str, score: float):
        """新增一条高质量示例，增量写入向量库并落盘"""
        self._ensure_loaded()

        # page_content：拼接问题、SQL、答案，用于语义检索
        page_content = f"问题: {question}\nSQL: {sql}\n答案: {answer[:500]}"

        metadata = {
            "question": question,
            "sql": sql,
            "tables": json.dumps(tables, ensure_ascii=False),
            "fields": json.dumps(fields, ensure_ascii=False),
            "domain": domain_tag,
            "score": score,
            "_placeholder": False,
        }

        doc = Document(page_content=page_content, metadata=metadata)
        self._vector_store.add_documents([doc])
        self._save_to_disk()

    def search_similar(self, question: str, current_tables: list = None, k: int = 3) -> list:
        """结构感知检索：语义相似 → 按表/字段重叠度重排序"""
        self._ensure_loaded()

        if self._vector_store is None:
            return []

        docs_with_scores = self._vector_store.similarity_search_with_score(question, k=k * 3)
        # 过滤占位文档
        candidates = [
            (doc, score) for doc, score in docs_with_scores
            if not doc.metadata.get("_placeholder")
        ]
        if not candidates:
            return []

        # 二阶段重排序：按结构重叠度
        if current_tables:
            scored = []
            for doc, score in candidates:
                doc_tables = json.loads(doc.metadata.get("tables", "[]"))
                doc_fields = json.loads(doc.metadata.get("fields", "[]"))
                # 重叠度 = 共同表/字段占比例
                table_overlap = len(set(doc_tables) & set(current_tables)) / max(len(current_tables), 1)
                field_overlap = 0
                if doc_fields:
                    field_overlap = len(set(doc_fields)) / max(len(doc_fields), 1)
                structure_score = (table_overlap + field_overlap) / 2
                # 综合排序：0.3 语义 + 0.7 结构
                combined = 0.3 * (1 - score / 2) + 0.7 * structure_score
                scored.append((doc, combined))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [doc for doc, _ in scored[:k]]

        # 无当前表信息时，纯语义排序
        candidates.sort(key=lambda x: x[1])
        return [doc for doc, _ in candidates[:k]]

    def _save_to_disk(self):
        """落盘到 cache/example_faiss_index"""
        os.makedirs(_CACHE_EXAMPLE_DIR, exist_ok=True)
        if self._vector_store is not None:
            self._vector_store.save_local(_CACHE_EXAMPLE_DIR)
