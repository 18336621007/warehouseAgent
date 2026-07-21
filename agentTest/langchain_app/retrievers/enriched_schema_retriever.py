"""基于增强元数据的 Schema 检索器，接口和 SchemaRetriever 完全一致，方便对比"""
class EnrichedSchemaRetriever:
    def __init__(self, vector_store, top_k=5):
        self.vector_store = vector_store
        self.top_k = top_k

    # 简要注释：把向量库转换成 LangChain 标准 retriever。
    def as_retriever(self):
        return self.vector_store.as_retriever(
            search_kwargs={"k": self.top_k}
        )

    # 执行检索器，根据 query 进行检索。
    def retrieve(self, query: str):
        retriever = self.as_retriever()
        return retriever.invoke(query)
