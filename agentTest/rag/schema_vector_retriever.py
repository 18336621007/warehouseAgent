class SchemaVectorRetriever:
    # SchemaVectorRetriever 负责基于向量索引召回最相关的 schema documents。

    def __init__(self, embedder, vector_index):
        self.embedder = embedder
        self.vector_index = vector_index

    def _score_row(self, query_vector: dict, row: dict) -> float:
        # 计算 query 与单个文档索引行的基础相似度分数。
        score = 0.0
        score += self.embedder.cosine_similarity(query_vector, row["table_name_vector"]) * 4.0
        score += self.embedder.cosine_similarity(query_vector, row["keywords_vector"]) * 3.0
        score += self.embedder.cosine_similarity(query_vector, row["column_names_vector"]) * 3.0
        score += self.embedder.cosine_similarity(query_vector, row["table_comment_vector"]) * 2.0
        score += self.embedder.cosine_similarity(query_vector, row["summary_vector"]) * 2.0
        score += self.embedder.cosine_similarity(query_vector, row["content_vector"]) * 1.0
        return score

    def _calculate_bonus(self, query: str, row: dict) -> float:
        # 基于业务词与关键字段命中情况追加轻量 bonus。
        normalized_query = str(query or "").lower()
        document = row["document"]

        table_name = str(document.get("table_name", "") or "").lower()
        content = str(document.get("content", "") or "").lower()
        column_names = " ".join(
            str(column.get("column_name", "") or "").lower()
            for column in document.get("columns", [])
        )

        bonus = 0.0

        if "订单" in normalized_query and "order" in table_name:
            bonus += 2.0

        if "金额" in normalized_query and any(
                token in column_names for token in ["amount", "amt", "pay_amt", "actual_amt", "gmv"]):
            bonus += 2.0

        if "支付" in normalized_query and any(
                token in (column_names + " " + content) for token in ["pay", "payment", "pay_time"]):
            bonus += 2.0

        if "用户" in normalized_query and any(token in column_names for token in ["user_id", "buyer_id"]):
            bonus += 2.0

        if "状态" in normalized_query and any(token in column_names for token in ["status", "state", "order_status"]):
            bonus += 2.0

        return bonus

    def retrieve(self, query: str, documents: list[dict], top_k: int = 3) -> list[dict]:
        # 根据 query 进行向量检索，并返回 top_k 文档。
        query_vector = self.embedder.embed(query)
        index_rows = self.vector_index.build_index(documents)

        scored_documents = []

        for row in index_rows:
            base_score = self._score_row(query_vector, row)
            bonus_score = self._calculate_bonus(query, row)
            final_score = base_score + bonus_score

            if final_score <= 0:
                continue

            result = dict(row["document"])
            result["base_score"] = base_score
            result["bonus_score"] = bonus_score
            result["score"] = final_score
            scored_documents.append(result)

        scored_documents.sort(key=lambda item: (-item["score"], item.get("doc_id", "")))
        return scored_documents[:top_k]