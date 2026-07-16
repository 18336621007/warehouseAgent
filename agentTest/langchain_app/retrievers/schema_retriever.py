
# 基于 schema vector store 提供检索入口
# 输入用户问题
# 返回相关 schema documents
class SchemaRetriever:
    def __init__(self, vector_store, top_k=5):
        self.vector_store = vector_store
        self.top_k = top_k

    # 简要注释：把向量库转换成 LangChain 标准 retriever。
    def as_retriever(self):

        return self.vector_store.as_retriever(
            search_kwargs = {"k": self.top_k}
        )

    # 执行检索器，根据query进行检索
    def retrieve(self, query: str):

        retriever = self.as_retriever()
        return retriever.invoke(query)