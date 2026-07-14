# SchemaDocumentRetriever 负责从 schema documents 中召回与当前问题最相关的文档。
# 它是当前第一版 schema RAG 的核心检索器，主要基于关键词和多字段打分，
# 返回最相关的 top_k schema 文档，供 planner prompt 做 grounding。
class SchemaDocumentRetriever:
    # 该类负责从 schema documents 中召回和问题最相关的文档
    """documents 的结构
    return {
            "doc_id": doc_id,
            "source": "hive_metadata",
            "database_name": database_name,
            "table_name": table_name,
            "table_comment": table_comment,
            "summary": summary,
            "content": content,
            "column_count": len(columns),
            "columns": columns,
            "keywords": keywords,
        }
    """
    KEYWORD_MAP = {
        "订单": ["order", "orders", "order_id", "order_status"],
        "金额": ["amount", "amt", "pay_amt", "actual_amt", "gmv"],
        "支付": ["pay", "payment", "pay_time", "pay_amt"],
        "时间": ["time", "date", "dt", "create_time", "pay_time"],
        "城市": ["city"],
        "状态": ["status", "state", "order_status"],
        "用户": ["user", "user_id", "buyer_id"],
    }

    def _normalize(self, text: str):
        # 统一文本格式，避免空值和大小写影响匹配结果。
        return str(text or "").strip().lower()

    def _score_document(self, query: str, document: dict):
        # 计算单条 schema document 与 query 的相关性分数。
        # 当前第一版主要参考 table_name、summary、content、keywords 四类信息。
        # 计算单条 document 的匹配分数
        table_name = self._normalize(document.get("table_name", ""))
        summary = self._normalize(document.get("summary", ""))
        content = self._normalize(document.get("content", ""))
        keywords = [self._normalize(keyword) for keyword in document.get("keywords", [])]

        score = 0

        for keyword, aliases in self.KEYWORD_MAP.items():
            if keyword not in query:
                continue

            for alias in aliases:
                if alias in table_name:
                    score += 4

                if any(alias in keyword_text for keyword_text in keywords):
                    score += 3

                if alias in summary:
                    score += 2

                if alias in content:
                    score += 1
        return score

    def retrieve(self, query: str, documents: list[dict], top_k: int = 3):
        # 根据 query 从 schema documents 中检索最相关的 top_k 文档：
        # 1. 对每个 document 计算匹配分数
        # 2. 过滤掉无命中的文档
        # 3. 按 score 排序返回 top_k
        # 4. 无明显命中时返回极少量兜底文档
        # 按 query 对 documents 打分并返回前 top_k 条结果
        normalized_query = self._normalize(query)
        scored_documents = []

        for document in documents:
            score = self._score_document(normalized_query, document)
            if score > 0:
                result = dict(document)
                result["score"] = score
                scored_documents.append(result)

        if not scored_documents:
            fallback_documents = []
            for document in documents[: min(top_k, 1)]:
                result = dict(document)
                result["score"] = 0
                fallback_documents.append(result)
            return fallback_documents

        scored_documents.sort(key=lambda item: (-item["score"], item.get("doc_id", "")))
        return scored_documents[:top_k]
