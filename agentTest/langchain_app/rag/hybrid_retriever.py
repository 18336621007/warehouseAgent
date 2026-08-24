# 混合检索调度器：融合 BM25 + 向量检索结果
# BM25 擅长精确关键词匹配，向量擅长语义相似度，两者互补
import os
from typing import List, Optional, Tuple, Dict, Any
import numpy as np

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore

from agentTest.langchain_app.rag.bm25_retriever import BM25Retriever
from agentTest.config.advisor import BM25_ALPHA


class HybridRetriever:
    """
    混合检索器：BM25 + 向量检索融合

    检索流程：
    1. 用户查询 -> BM25 检索 + 向量检索（并行）
    2. 分数归一化（Min-Max）
    3. 加权融合：alpha * BM25 + (1-alpha) * VEC
    4. 返回融合后的 top_k 结果

    配置：
    - BM25_ALPHA: BM25 在混合检索中的权重
      - 0.0 = 只用向量检索
      - 1.0 = 只用 BM25 检索
      - 0.3 = 30% BM25 + 70% 向量（默认）
    """

    def __init__(
        self,
        bm25_retriever: Optional[BM25Retriever] = None,
        vector_stores: Optional[Dict[str, VectorStore]] = None,
        alpha: Optional[float] = None,
    ):
        """
        初始化混合检索器

        Args:
            bm25_retriever: BM25 检索器实例
            vector_stores: 向量库字典 {"db": vs, "table": vs, "column": vs}
            alpha: BM25 权重，默认从配置读取
        """
        self.bm25_retriever = bm25_retriever
        self.vector_stores = vector_stores or {}
        self.alpha = alpha if alpha is not None else BM25_ALPHA

    def _normalize_scores(self, scores: np.ndarray) -> np.ndarray:
        """
        Min-Max 归一化分数到 [0, 1]

        Args:
            scores: 原始分数数组

        Returns:
            归一化后的分数数组
        """
        min_score = scores.min()
        max_score = scores.max()

        if max_score == min_score:
            # 所有分数相同，返回全 0 或全 1
            return np.zeros_like(scores) if max_score == 0 else np.ones_like(scores)

        return (scores - min_score) / (max_score - min_score)

    def _fuse_scores(
        self,
        bm25_scores: Dict[str, float],
        vec_scores: Dict[str, float],
        all_doc_ids: List[str],
    ) -> List[Tuple[str, float]]:
        """
        加权融合 BM25 和向量分数

        Args:
            bm25_scores: {doc_id: bm25_score}
            vec_scores: {doc_id: vector_score}
            all_doc_ids: 所有候选文档 ID

        Returns:
            融合后的 (doc_id, fused_score) 列表，按分数降序
        """
        fused = {}

        for doc_id in all_doc_ids:
            bm25 = bm25_scores.get(doc_id, 0.0)
            vec = vec_scores.get(doc_id, 0.0)

            # 加权融合
            fused[doc_id] = self.alpha * bm25 + (1 - self.alpha) * vec

        # 按分数降序排序
        sorted_items = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        return sorted_items

    def _get_doc_id(self, doc: Document) -> str:
        """
        生成文档唯一标识（用于分数融合时去重）

        Args:
            doc: Document 对象

        Returns:
            文档唯一 ID
        """
        meta = doc.metadata
        if meta.get("column") and meta.get("table"):
            return f"{meta.get('table')}.{meta.get('column')}"
        if meta.get("table"):
            return meta.get("table")
        if meta.get("database"):
            return meta.get("database")
        return id(doc)

    def search_databases(
        self,
        query: str,
        k: int = 5,
        vector_store_key: str = "db",
    ) -> List[Document]:
        """
        检索数据库/库

        Args:
            query: 用户查询
            k: 返回结果数量
            vector_store_key: 向量库 key

        Returns:
            检索结果列表
        """
        results = self._search(
            query=query,
            k=k,
            vector_store_key=vector_store_key,
        )
        return [doc for doc, _ in results]

    def search_tables(
        self,
        query: str,
        k: int = 5,
        vector_store_key: str = "table",
    ) -> List[Document]:
        """
        检索表

        Args:
            query: 用户查询
            k: 返回结果数量
            vector_store_key: 向量库 key

        Returns:
            检索结果列表
        """
        results = self._search(
            query=query,
            k=k,
            vector_store_key=vector_store_key,
        )
        return [doc for doc, _ in results]

    def search_columns(
        self,
        query: str,
        k: int = 5,
        vector_store_key: str = "column",
    ) -> List[Document]:
        """
        检索字段

        Args:
            query: 用户查询
            k: 返回结果数量
            vector_store_key: 向量库 key

        Returns:
            检索结果列表
        """
        results = self._search(
            query=query,
            k=k,
            vector_store_key=vector_store_key,
        )
        return [doc for doc, _ in results]

    def _search(
        self,
        query: str,
        k: int,
        vector_store_key: str,
    ) -> List[Tuple[Document, float]]:
        """
        核心检索方法：BM25 + 向量融合

        Args:
            query: 用户查询
            k: 返回结果数量
            vector_store_key: 向量库 key

        Returns:
            List[(Document, fused_score)]
        """
        # 1. BM25 检索
        bm25_results = {}
        if self.bm25_retriever:
            bm25_raw = self.bm25_retriever.retrieve(query, top_k=k * 3)
            for doc, score in bm25_raw:
                doc_id = self._get_doc_id(doc)
                bm25_results[doc_id] = score

        # 2. 向量检索
        vec_results = {}
        vector_store = self.vector_stores.get(vector_store_key)

        if vector_store:
            # 向量检索通常返回 (doc, score) 元组
            vec_docs = vector_store.similarity_search_with_score(query, k=k * 3)
            for doc, score in vec_docs:
                doc_id = self._get_doc_id(doc)
                vec_results[doc_id] = score

        # 3. 收集所有候选文档
        all_doc_ids = set(list(bm25_results.keys()) + list(vec_results.keys()))

        if not all_doc_ids:
            return []

        # 4. 构建 (doc_id -> doc) 映射
        doc_map = {}
        for doc_id in all_doc_ids:
            # 从 BM25 结果或向量结果中获取 Document
            for doc, _ in bm25_raw or []:
                if self._get_doc_id(doc) == doc_id:
                    doc_map[doc_id] = doc
                    break
            if doc_id not in doc_map:
                for doc, _ in vec_docs or []:
                    if self._get_doc_id(doc) == doc_id:
                        doc_map[doc_id] = doc
                        break

        # 5. 归一化并融合分数
        # BM25 分数归一化
        if bm25_results:
            bm25_scores = np.array(list(bm25_results.values()))
            bm25_normalized = self._normalize_scores(bm25_scores)
            bm25_results = {
                doc_id: float(score)
                for doc_id, score in zip(bm25_results.keys(), bm25_normalized)
            }

        # 向量分数归一化（默认是距离，需要转相似度）
        if vec_results:
            vec_scores = np.array(list(vec_results.values()))
            # 如果是距离，转换为相似度；如果是相似度，直接归一化
            vec_normalized = self._normalize_scores(vec_scores)
            vec_results = {
                doc_id: float(score)
                for doc_id, score in zip(vec_results.keys(), vec_normalized)
            }

        # 6. 加权融合
        fused_scores = self._fuse_scores(bm25_results, vec_results, list(all_doc_ids))

        # 7. 返回 top_k
        results = []
        for doc_id, fused_score in fused_scores[:k]:
            if doc_id in doc_map:
                results.append((doc_map[doc_id], fused_score))

        return results

    def search(
        self,
        query: str,
        k: int = 5,
        include_vector: bool = True,
        include_bm25: bool = True,
    ) -> List[Tuple[Document, float, Dict[str, float]]]:
        """
        通用检索接口（返回详细分数信息）

        Args:
            query: 用户查询
            k: 返回结果数量
            include_vector: 是否包含向量检索
            include_bm25: 是否包含 BM25 检索

        Returns:
            List[(Document, fused_score, {"bm25": x, "vector": y})]
        """
        # BM25 检索
        bm25_results = {}
        if include_bm25 and self.bm25_retriever:
            bm25_raw = self.bm25_retriever.retrieve(query, top_k=k * 3)
            for doc, score in bm25_raw:
                doc_id = self._get_doc_id(doc)
                bm25_results[doc_id] = score

        # 向量检索
        vec_results = {}
        if include_vector and self.vector_stores:
            # 合并所有向量库的检索结果
            for key, vs in self.vector_stores.items():
                vec_docs = vs.similarity_search_with_score(query, k=k)
                for doc, score in vec_docs:
                    doc_id = self._get_doc_id(doc)
                    vec_results[doc_id] = max(vec_results.get(doc_id, 0), score)

        # 收集所有候选
        all_doc_ids = list(set(list(bm25_results.keys()) + list(vec_results.keys())))

        if not all_doc_ids:
            return []

        # 构建 doc_map
        doc_map = {}
        for doc_id in all_doc_ids:
            for doc, _ in bm25_raw or []:
                if self._get_doc_id(doc) == doc_id:
                    doc_map[doc_id] = doc
                    break
            if doc_id not in doc_map:
                for key, vs in self.vector_stores.items():
                    vec_docs = vs.similarity_search_with_score(query, k=100)
                    for doc, _ in vec_docs:
                        if self._get_doc_id(doc) == doc_id:
                            doc_map[doc_id] = doc
                            break

        # 归一化
        if bm25_results:
            scores = np.array(list(bm25_results.values()))
            bm25_results = {
                doc_id: float(s)
                for doc_id, s in zip(bm25_results.keys(), self._normalize_scores(scores))
            }

        if vec_results:
            scores = np.array(list(vec_results.values()))
            vec_results = {
                doc_id: float(s)
                for doc_id, s in zip(vec_results.keys(), self._normalize_scores(scores))
            }

        # 融合
        fused_scores = self._fuse_scores(bm25_results, vec_results, all_doc_ids)

        # 返回
        results = []
        for doc_id, fused_score in fused_scores[:k]:
            if doc_id in doc_map:
                details = {
                    "bm25": bm25_results.get(doc_id, 0.0),
                    "vector": vec_results.get(doc_id, 0.0),
                }
                results.append((doc_map[doc_id], fused_score, details))

        return results

    def set_alpha(self, alpha: float) -> None:
        """
        动态调整 BM25 权重

        Args:
            alpha: BM25 权重 [0, 1]
        """
        self.alpha = max(0.0, min(1.0, alpha))

    def get_alpha(self) -> float:
        """获取当前 BM25 权重"""
        return self.alpha
