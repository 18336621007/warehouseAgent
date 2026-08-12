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
        """惰性构建当前白名单内可访问的标识集合（db.table 或 database）。"""
        if self._allowed_identifiers is not None or self._use_config_fallback:
            return self._allowed_identifiers
        try:
            if self._key == "database":
                # 库级过滤只依赖配置，无需访问 Hive
                self._allowed_identifiers = set(
                    str(db).lower() for db in get_allowed_databases()
                )
            else:
                tables = self._metadata_provider.list_tables()
                self._allowed_identifiers = {
                    f"{table.get('database_name', '')}.{table.get('table_name', '')}"
                    for table in tables
                }
        except Exception:
            # Hive 暂不可用时退化为配置级判定，避免整体召回不可用
            self._use_config_fallback = True
            self._allowed_identifiers = None
        return self._allowed_identifiers

    def _is_allowed_identifier(self, identifier: str) -> bool:
        """判断表/库标识是否在当前接入范围内。"""
        identifier = str(identifier or "").strip()
        if not identifier:
            return True
        allowed = self._allowed_set()
        if allowed is not None:
            return identifier.lower() in allowed
        # 配置兜底：拆 db.table 后按 metadata_scope 判定
        if self._key == "database":
            return identifier.lower() in set(
                str(db).lower() for db in get_allowed_databases()
            )
        db, _, table = identifier.rpartition(".")
        if not db or not table:
            return True
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

    def similarity_search(self, query, k=4, **kwargs):
        docs = self._inner_store.similarity_search(query, k=k, **kwargs)
        return self._filter_docs(docs)

    def similarity_search_with_score(self, query, k=4, **kwargs):
        docs = self._inner_store.similarity_search_with_score(query, k=k, **kwargs)
        return self._filter_docs_with_score(docs)
