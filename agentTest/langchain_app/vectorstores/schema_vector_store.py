from langchain_community.vectorstores import FAISS
# 管理 schema document进入向量库
# 封装向量库初始化、写入、加载
# 提供统一的 vector store 获取方式

class SchemaVectorStore():
    def __init__(self, embeddings):
        self.embeddings = embeddings

    def build(self, documents):
        return FAISS.from_documents(documents, self.embeddings)