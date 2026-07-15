class SchemaVectorRetriever:
    # SchemaVectorRetriever 负责基于向量索引召回最相关的 schema documents。

    def __init__(self, embedder, vector_index):
        self.embedder = embedder
        self.vector_index = vector_index

    def retrieve(self, query: str, documents: list[dict], top_k: int = 3) -> list[dict]:
        # 根据 query 进行向量检索，并返回 top_k 文档。
        query_vector = self.embedder.embed(query)
        index_rows = self.vector_index.build_index(documents)

        scored_documents = []

        for row in index_rows:
            score = self.embedder.cosine_similarity(query_vector, row["vector"]) #计算余弦相似度
            if score <= 0:
                continue

            result = dict(row["document"])
            result["score"] = score
            scored_documents.append(result)

        scored_documents.sort(key=lambda item: (-item["score"], item.get("doc_id", "")))

        return scored_documents[:top_k]