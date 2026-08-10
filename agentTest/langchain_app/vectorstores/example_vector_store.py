# 高质量对话示例向量库 —— 存储 Evaluator >= 80 分的对话
# v2: 新增 hash_id 去重、remove_example 删除、sync_by_score 同步
import os, json, hashlib
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_community.vectorstores.utils import DistanceStrategy
from agentTest.config.evaluator import EXAMPLE_DEDUP_SIMILARITY
from agentTest.config.planner import EXAMPLE_SIMILARITY_THRESHOLD

# 3级向上到agentTest目录，与其他FAISS缓存同路径
_CACHE_EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "langgraph_app", "cache", "example_faiss_index")


def example_hash_id(question: str) -> str:
    """按规范化问题原文生成唯一标识，用于优秀案例按问题去重"""
    normalized = " ".join(str(question or "").split())
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


class ExampleVectorStore:
    """管理高质量对话示例的 FAISS 向量库，支持增量写入、去重和结构感知检索"""

    def __init__(self, embeddings):
        self.embeddings = embeddings
        self._vector_store = None

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
                    tables: list, fields: list, domain_tag: str, score: float,
                    effective_query: str = ""):
        """新增高质量示例。按问题去重（精确 hash + 语义 0.9 且表/字段一致），高分优先。"""
        self._ensure_loaded()
        hash_id = example_hash_id(question)

        # 去重1: 精确 hash 匹配（相同问题原文只存一份）
        for doc_id, doc in self._vector_store.docstore._dict.items():
            if doc.metadata.get("hash_id") == hash_id:
                self._merge_or_update(doc, question, sql, answer, tables, fields,
                                      domain_tag, score, effective_query, hash_id)
                self._save_to_disk()
                return

        # 去重2: 语义相似（≥ 阈值）且表/字段一致视为同一问题
        existing = self._vector_store.similarity_search_with_score(question, k=1)
        if existing:
            top_doc, top_score = existing[0]
            if not top_doc.metadata.get("_placeholder"):
                similarity = 1 - top_score / 2
                doc_tables = json.loads(top_doc.metadata.get("tables", "[]"))
                doc_fields = json.loads(top_doc.metadata.get("fields", "[]"))
                same_structure = (
                    set(doc_tables) == set(tables) and set(doc_fields) == set(fields)
                )
                if similarity >= EXAMPLE_DEDUP_SIMILARITY and same_structure:
                    self._merge_or_update(top_doc, question, sql, answer, tables, fields,
                                          domain_tag, score, effective_query, hash_id)
                    self._save_to_disk()
                    return

        # 写入新记录（page_content 用原文，便于语义召回）
        page_content = f"问题: {question}\nSQL: {sql}\n答案: {answer[:500]}"
        metadata = {
            "question": question,
            "effective_query": effective_query or question,
            "original_question": question,
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

    def _merge_or_update(self, doc, question, sql, answer, tables, fields,
                         domain_tag, score, effective_query, hash_id):
        """高分优先合并：新案例分更高则整体替换，否则保留高分内容仅更新分数。"""
        old_score = doc.metadata.get("score", 0)
        if score >= old_score:
            doc.metadata.update({
                "question": question,
                "effective_query": effective_query or question,
                "original_question": question,
                "sql": sql,
                "tables": json.dumps(tables, ensure_ascii=False),
                "fields": json.dumps(fields, ensure_ascii=False),
                "domain": domain_tag,
                "score": score,
                "hash_id": hash_id,
            })
            doc.page_content = f"问题: {question}\nSQL: {sql}\n答案: {answer[:500]}"
        else:
            doc.metadata["score"] = max(old_score, score)

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
                      score: float, is_high: bool, effective_query: str = ""):
        """根据评分结果同步 FAISS：高分则加，低分则删"""
        if is_high:
            self.add_example(question, sql, answer, tables, fields, domain_tag, score,
                             effective_query=effective_query)
        else:
            self.remove_example(hash_id)

    def search_similar(self, question: str, current_tables: list = None, k: int = 3, min_similarity: float = EXAMPLE_SIMILARITY_THRESHOLD) -> list:
        """结构感知检索：语义相似 -> 按表/字段重叠度重排序"""
        self._ensure_loaded()
        if self._vector_store is None:
            return []
        docs_with_scores = self._vector_store.similarity_search_with_score(question, k=k * 3)
        candidates = [(doc, score) for doc, score in docs_with_scores
                      if not doc.metadata.get("_placeholder")
                      and (1 - score / 2) >= min_similarity]
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
            # _similarity 已在 candidates 处理阶段写入，此处直接返回
            return [doc for doc, _ in scored[:k]]
        candidates.sort(key=lambda x: x[1])
        # 将相似度写入 metadata 供日志使用
        for doc, score in candidates[:k]:
            doc.metadata["_similarity"] = round(1 - score / 2, 3)
        return [doc for doc, _ in candidates[:k]]

    def _save_to_disk(self):
        """落盘到 cache/example_faiss_index"""
        os.makedirs(_CACHE_EXAMPLE_DIR, exist_ok=True)
        if self._vector_store is not None:
            self._vector_store.save_local(_CACHE_EXAMPLE_DIR)
