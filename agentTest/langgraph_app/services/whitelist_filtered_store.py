# 向量召回白名单过滤包装器：保证召回给模型/程序的表都位于当前接入白名单内
# 增强元数据/向量库可能存在已移出白名单的陈旧表，直接召回会让模型锁定不可查询的表
from agentTest.db.metadata_scope import get_allowed_databases
from agentTest.db.metadata_scope import is_allowed_table


class WhitelistFilteredVectorStore:
    """包装 db/table/column 三层 FAISS，按当前接入白名单过滤召回结果。"""

    def __init__(self, inner_store, metadata_provider, key="table"):
        self._inner_store = inner_store
        self._metadata_provider = metadata_provider
        # 表级/字段级元数据键为 table，库级元数据键为 database
        self._key = key
        self._allowed_identifiers = None  # 权威白名单集合，惰性构建
        self._use_config_fallback = False  # Hive 不可用时退化为纯配置判定

    def _allowed_set(self):
        """返回 None：标识判定直接走 metadata_scope 配置（不依赖 Hive 表清单缓存）。"""
        return None

    def _is_allowed_identifier(self, identifier: str) -> bool:
        """判断表/库标识是否在当前接入范围内，直接按 metadata_scope 配置判定。"""
        identifier = str(identifier or "").strip()
        if not identifier:
            return True
        if self._key == "database":
            return identifier.lower() in set(
                str(db).lower() for db in get_allowed_databases()
            )
        db, _, table = identifier.rpartition(".")
        if not db or not table:
            # 无库名标识（历史数据/裸表名）：按裸表名配置判定
            return is_allowed_table(table, "")
        return is_allowed_table(table, db)

    def _filter_docs(self, docs):
        return [
            doc
            for doc in docs
            if self._is_allowed_identifier(
                str((doc.metadata or {}).get(self._key, ""))
            )
        ]

    def _filter_docs_with_score(self, docs_with_scores):
        return [
            (doc, score)
            for doc, score in docs_with_scores
            if self._is_allowed_identifier(
                str((doc.metadata or {}).get(self._key, ""))
            )
        ]

    def _table_documents(self, table: str) -> list:
        """精确返回某表的全部 Document（直接查 docstore，不经过向量相似度截断）。"""
        docstore = getattr(getattr(self._inner_store, "docstore", None), "_dict", None)
        if docstore is None:
            return []
        return [
            doc for doc in docstore.values()
            if str((doc.metadata or {}).get("table", "")) == table
        ]

    def columns_in_table(self, table: str) -> list[str]:
        """精确返回某表在当前白名单内的字段名列表（供字段存在性判断，避免 fetch_k 截断漏检）。"""
        table = str(table or "")
        if not self._is_allowed_identifier(table):
            return []
        return [
            str(doc.metadata.get("column", ""))
            for doc in self._table_documents(table)
            if doc.metadata.get("column")
        ]

    def documents_by_table(self, table: str) -> list:
        """精确返回某表在当前白名单内的字段 Document 列表（供按表召回字段，避免向量检索漏召）。"""
        table = str(table or "")
        if not self._is_allowed_identifier(table):
            return []
        return [
            doc for doc in self._table_documents(table)
            if doc.metadata.get("column")
        ]

    def similarity_search(self, query, k=4, **kwargs):
        docs = self._inner_store.similarity_search(query, k=k, **kwargs)
        return self._filter_docs(docs)

    def similarity_search_with_score(self, query, k=4, **kwargs):
        docs = self._inner_store.similarity_search_with_score(query, k=k, **kwargs)
        return self._filter_docs_with_score(docs)
