# BM25 倒排索引检索器，使用 rank_bm25 库实现关键词精确匹配
# 与 FAISS 向量检索互补，BM25 擅长精确关键词匹配，向量擅长语义相似度
import os
import pickle
import jieba
from typing import List, Optional, Tuple

from langchain_core.documents import Document

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    raise ImportError("请安装 rank-bm25: pip install rank-bm25")


# 中文停用词表（常用停用词）
STOPWORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
    "自己", "这", "那", "他", "她", "它", "们", "这个", "那个", "什么", "怎么",
    "表名", "表说明", "字段", "字段信息", "string", "int", "bigint", "varchar",
    "datetime", "timestamp", "decimal", "boolean",
}


class BM25Retriever:
    """
    BM25 倒排索引检索器

    索引结构：
    - 倒排索引：词 -> [(doc_idx, tf), ...]
    - IDF 表：词 -> idf 值
    - 语料统计：文档总数、平均长度等

    检索流程：
    1. 用户查询 -> jieba 分词
    2. 查倒排索引，找每词命中文档
    3. 计算 BM25 得分（TF 饱和 + 长度归一化）
    4. 返回 top_k 文档及分数
    """

    def __init__(self):
        self.bm25: Optional[BM25Okapi] = None
        self.corpus: List[Document] = []
        self.tokenized_corpus: List[List[str]] = []

    def _tokenize(self, text: str) -> List[str]:
        """
        中文分词 + 停用词过滤

        Args:
            text: 原始文本

        Returns:
            分词后的词列表
        """
        # jieba 分词
        tokens = jieba.cut(text)
        # 停用词过滤 + 去除空字符串
        return [t.strip() for t in tokens if t.strip() and t not in STOPWORDS]

    def build_index(self, documents: List[Document]) -> None:
        """
        从文档列表构建 BM25 索引

        Args:
            documents: Document 对象列表（与 FAISS 共用同一数据源）
        """
        if not documents:
            raise ValueError("文档列表为空，无法构建索引")

        self.corpus = documents

        # 分词语料库
        self.tokenized_corpus = [self._tokenize(doc.page_content) for doc in documents]

        # 构建 BM25 索引（rank_bm25 内部计算全局 IDF）
        self.bm25 = BM25Okapi(self.tokenized_corpus)

    def retrieve(self, query: str, top_k: int = 5) -> List[Tuple[Document, float]]:
        """
        BM25 检索

        Args:
            query: 用户查询文本
            top_k: 返回前 k 条结果

        Returns:
            List[(Document, bm25_score)]，按得分降序排列
        """
        if self.bm25 is None:
            raise ValueError("索引未构建，请先调用 build_index()")

        # 查询分词
        query_tokens = self._tokenize(query)

        # 计算 BM25 得分（每个文档的得分）
        scores = self.bm25.get_scores(query_tokens)

        # 获取 top_k 文档索引
        # scores 是 numpy array，按降序排列取前 k 个
        import numpy as np
        top_indices = np.argsort(scores)[::-1][:top_k]

        # 返回 (文档, 得分) 列表
        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score > 0:  # 只返回有匹配的文档
                results.append((self.corpus[idx], score))

        return results

    def retrieve_with_scores(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0
    ) -> List[Tuple[Document, float]]:
        """
        BM25 检索（带最低分过滤）

        Args:
            query: 用户查询文本
            top_k: 返回前 k 条结果
            min_score: 最低分阈值，只返回分数 >= min_score 的文档

        Returns:
            List[(Document, bm25_score)]，按得分降序排列
        """
        results = self.retrieve(query, top_k * 2)  # 多取一些以便过滤
        return [(doc, score) for doc, score in results if score >= min_score][:top_k]

    def save(self, path: str) -> None:
        """
        保存索引到本地磁盘

        Args:
            path: 缓存目录路径
        """
        os.makedirs(path, exist_ok=True)

        # 保存 BM25 对象
        with open(os.path.join(path, "bm25.pkl"), "wb") as f:
            pickle.dump(self.bm25, f)

        # 保存语料（需要保留原始 Document 对象以便返回）
        with open(os.path.join(path, "corpus.pkl"), "wb") as f:
            pickle.dump(self.corpus, f)

        # 保存分词后的语料
        with open(os.path.join(path, "tokenized_corpus.pkl"), "wb") as f:
            pickle.dump(self.tokenized_corpus, f)

    def load(self, path: str) -> None:
        """
        从本地磁盘加载索引

        Args:
            path: 缓存目录路径
        """
        with open(os.path.join(path, "bm25.pkl"), "rb") as f:
            self.bm25 = pickle.load(f)

        with open(os.path.join(path, "corpus.pkl"), "rb") as f:
            self.corpus = pickle.load(f)

        with open(os.path.join(path, "tokenized_corpus.pkl"), "rb") as f:
            self.tokenized_corpus = pickle.load(f)

    @property
    def index_size(self) -> int:
        """返回索引中的文档数量"""
        return len(self.corpus)

    def get_vocabulary_size(self) -> int:
        """返回词表大小（不同词的数量）"""
        if self.tokenized_corpus:
            vocab = set()
            for tokens in self.tokenized_corpus:
                vocab.update(tokens)
            return len(vocab)
        return 0
