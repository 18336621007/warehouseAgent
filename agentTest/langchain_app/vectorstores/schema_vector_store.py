# 管理 schema document 进入向量库
# 封装向量库初始化、写入、加载
# 提供统一的 vector store 获取方式，支持本地磁盘缓存避免重复 embedding
import os
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy  # 余弦相似度，更适合语义检索


class SchemaVectorStore():
    def __init__(self, embeddings):
        self.embeddings = embeddings

    def build(self, documents):
        """从文档列表构建 FAISS 向量库（不落盘），采用余弦相似度，更适合语义检索"""
        return FAISS.from_documents(documents, self.embeddings, distance_strategy=DistanceStrategy.COSINE)

    # 简要注释：保存向量库到本地磁盘目录。
    def save(self, vector_store, path: str):
        os.makedirs(path, exist_ok=True)
        vector_store.save_local(path)

    # 简要注释：优先从本地磁盘加载向量库；不存在则从文档构建并落盘。
    def load_or_build(self, path: str, documents):
        if os.path.exists(path) and os.listdir(path):
            try:
                print(f"  -> 从磁盘加载向量库: {path}")
                return FAISS.load_local(path, self.embeddings, allow_dangerous_deserialization=True)
            except Exception:
                # 旧 L2 缓存加载失败，重建
                print(f"  -> 加载失败（可能是旧 L2 缓存），将重建: {path}")
        print(f"  -> 构建向量库并落盘: {path}")
        vector_store = self.build(documents)
        self.save(vector_store, path)
        return vector_store
