# 简要注释：百炼 Embeddings 适配器，负责把文本按百炼兼容接口要求转换为向量。

import os

from openai import OpenAI


class BailianEmbeddings:
    # 简要注释：初始化百炼 embeddings 客户端。
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
        self.model = os.getenv("EMBEDDING_MODEL")

    # 简要注释：批量文本向量化，供向量库建索引使用。
    def embed_documents(self, texts):
        response = self.client.embeddings.create(
            model=self.model,
            input=texts,
            encoding_format="float",
        )
        return [item.embedding for item in response.data]

    # 简要注释：单条查询文本向量化，供检索阶段使用。
    def embed_query(self, text):
        response = self.client.embeddings.create(
            model=self.model,
            input=text,
            encoding_format="float",
        )
        return response.data[0].embedding

    # 简要注释：兼容部分向量库把 embeddings 对象当可调用对象使用的情况。
    def __call__(self, text):
        return self.embed_query(text)