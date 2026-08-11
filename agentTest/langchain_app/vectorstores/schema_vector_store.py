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

    def _existing_key_to_ids(self, vector_store) -> dict:
        """返回 {文档唯一键: [docstore_id]}，供更新/删除定位旧向量"""
        mapping = {}
        for doc_id in vector_store.index_to_docstore_id.values():
            doc = vector_store.docstore.search(doc_id)
            if doc and doc.metadata:
                key = self._doc_key(doc)
                mapping.setdefault(key, []).append(doc_id)
        return mapping

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

    # 简要注释：同步 MySQL 文档到本地向量库（M3 upsert/delete）。
    # force_rebuild=True 时删除缓存全量重建；否则按唯一键同步：
    # - 新增 key → 追加
    # - 已有 key 内容变化 → 删除旧向量后重写
    # - 消失的 key → 删除向量
    def sync_documents(self, path: str, documents, force_rebuild: bool = False, return_stats: bool = False):
        # 变更统计：rebuilt=全量重建；added/changed/removed=增量条数
        stats = {"rebuilt": False, "added": 0, "changed": 0, "removed": 0}

        if force_rebuild and os.path.exists(path):
            import shutil
            shutil.rmtree(path)

        if not os.path.exists(path) or not os.listdir(path):
            # 延迟导入，避免循环依赖
            from agentTest.langgraph_app.runtime.graph_logger import log_node_event
            log_node_event("vector_store", f"构建并落盘: {path}")
            vector_store = self.build(documents)
            self.save(vector_store, path)
            stats["rebuilt"] = True
            stats["added"] = len(documents)
            if return_stats:
                return vector_store, stats
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
            stats["rebuilt"] = True
            stats["added"] = len(documents)
            if return_stats:
                return vector_store, stats
            return vector_store

        existing = self._existing_key_to_ids(vs)
        new_docs = [d for d in documents if self._doc_key(d) not in existing]
        by_key = {self._doc_key(d): d for d in documents}

        # 内容变化检测：唯一键相同但 page_content 不同 → 删除旧向量后重写
        changed_docs = []
        changed_ids = []
        for key, ids in existing.items():
            if key not in by_key:
                continue  # 已消失，走删除分支
            old_doc = vs.docstore.search(ids[0]) if ids else None
            if old_doc is not None and old_doc.page_content != by_key[key].page_content:
                changed_ids.extend(ids)
                changed_docs.append(by_key[key])

        # 消失的 key：从向量库删除
        removed_ids = [
            doc_id for key, ids in existing.items()
            if key not in by_key
            for doc_id in ids
        ]

        # 增量统计：本次新增/更新/删除条数
        stats["added"] = len(new_docs)
        stats["changed"] = len(changed_docs)
        stats["removed"] = len(removed_ids)

        from agentTest.langgraph_app.runtime.graph_logger import log_node_event
        if removed_ids:
            log_node_event("vector_store", f"删除 {len(removed_ids)} 条（MySQL 已消失）: {path}")
            vs.delete(ids=removed_ids)
        if changed_ids:
            log_node_event("vector_store", f"更新 {len(changed_docs)} 条（内容变化）: {path}")
            vs.delete(ids=changed_ids)
            vs.add_documents(changed_docs)
        if new_docs:
            log_node_event("vector_store", f"增量追加 {len(new_docs)} 条到: {path}")
            vs.add_documents(new_docs)

        if removed_ids or changed_ids or new_docs:
            self.save(vs, path)
        else:
            log_node_event("vector_store", f"无变化，直接加载: {path}")

        if return_stats:
            return vs, stats
        return vs

    # 简要注释：优先从本地磁盘加载向量库；不存在则从文档构建并落盘。
    # 语义与 sync_documents 一致（M3 后默认按唯一键增量同步）。
    def load_or_build(self, path: str, documents, force_rebuild: bool = False, return_stats: bool = False):
        return self.sync_documents(path, documents, force_rebuild=force_rebuild, return_stats=return_stats)
