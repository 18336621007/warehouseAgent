import copy


class SchemaVectorIndex:
    # SchemaVectorIndex 负责将 schema documents 转为分字段向量索引，并提供缓存。

    def __init__(self, embedder):
        self.embedder = embedder
        self._index_cache = None  # 向量索引缓存
        self._build_index_count = 0  # 统计索引是否命中缓存

    def _build_column_names_text(self, document: dict) -> str:
        # 提取所有列名，供字段级检索使用。
        columns = document.get("columns", [])
        return " ".join(
            str(column.get("column_name", "") or "")
            for column in columns
        )

    def build_index(self, documents: list[dict]) -> list[dict]:
        # 将 documents 构建为分字段向量索引，并缓存索引结果。
        if self._index_cache is not None:
            return copy.deepcopy(self._index_cache)

        self._build_index_count += 1
        index_rows = []

        for document in documents:
            table_name_text = str(document.get("table_name", "") or "")
            table_comment_text = str(document.get("table_comment", "") or "")
            summary_text = str(document.get("summary", "") or "")
            keywords_text = " ".join(document.get("keywords", []))
            content_text = str(document.get("content", "") or "")
            column_names_text = self._build_column_names_text(document)

            index_rows.append(
                {
                    "doc_id": document.get("doc_id", ""),
                    "document": dict(document),
                    "table_name_vector": self.embedder.embed(table_name_text),
                    "table_comment_vector": self.embedder.embed(table_comment_text),
                    "summary_vector": self.embedder.embed(summary_text),
                    "keywords_vector": self.embedder.embed(keywords_text),
                    "content_vector": self.embedder.embed(content_text),
                    "column_names_vector": self.embedder.embed(column_names_text),
                }
            )

        self._index_cache = index_rows
        return copy.deepcopy(self._index_cache)

    def clear_cache(self):
        # 清空索引缓存，供测试或元数据刷新后重建。
        self._index_cache = None
