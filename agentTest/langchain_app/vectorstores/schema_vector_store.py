# 管理 schema document 进入向量库
# 封装向量库初始化、写入、加载
# 提供统一的 vector store 获取方式，支持本地磁盘缓存避免重复 embedding
# 支持增量追加：加载已有缓存后只对新文档做 embedding
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

    def _existing_keys(self, vector_store) -> set[str]:
        """从已加载的 FAISS 实例中提取已有文档的唯一键集合"""
        keys = set()
        for doc_id in vector_store.index_to_docstore_id.values():
            doc = vector_store.docstore.search(doc_id)
            if doc and doc.metadata:
                keys.add(self._doc_key(doc))
        return keys

    def _doc_key(self, doc) -> str:
        """文档唯一键：column 层用 table.column，table 层用 table，db 层用 database"""
        meta = doc.metadata
        column = meta.get("column", "")
        table = meta.get("table", "")
        database = meta.get("database", "")
        if column and table:
            return f"{table}.{column}"
        if table:
            return table
        return database or ""

    # 简要注释：优先从本地磁盘加载向量库；不存在则从文档构建并落盘。
    # force_rebuild=True 时删除缓存全量重建；否则增量追加新文档。
    def load_or_build(self, path: str, documents, force_rebuild: bool = False):
        if force_rebuild and os.path.exists(path):
            import shutil
            shutil.rmtree(path)

        if not os.path.exists(path) or not os.listdir(path):
            # 延迟导入，避免循环依赖
            from agentTest.langgraph_app.runtime.graph_logger import log_node_event
            log_node_event("vector_store", f"构建并落盘: {path}")
            vector_store = self.build(documents)
            self.save(vector_store, path)
            return vector_store

        # 加载已有缓存，对比新文档
        try:
            from agentTest.langgraph_app.runtime.graph_logger import log_node_event
            vs = FAISS.load_local(path, self.embeddings, allow_dangerous_deserialization=True)
        except Exception:
            from agentTest.langgraph_app.runtime.graph_logger import log_node_event
            log_node_event("vector_store", f"加载失败（旧缓存），重建: {path}")
            vector_store = self.build(documents)
            self.save(vector_store, path)
            return vector_store

        existing_keys = self._existing_keys(vs)
        new_docs = [d for d in documents if self._doc_key(d) not in existing_keys]

        if new_docs:
            from agentTest.langgraph_app.runtime.graph_logger import log_node_event
            log_node_event("vector_store", f"增量追加 {len(new_docs)} 条到: {path}")
            vs.add_documents(new_docs)
            self.save(vs, path)
        else:
            from agentTest.langgraph_app.runtime.graph_logger import log_node_event
            log_node_event("vector_store", f"无新增，直接加载: {path}")

        return vs
