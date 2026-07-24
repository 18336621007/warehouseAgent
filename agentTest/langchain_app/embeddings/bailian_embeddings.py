# 简要注释：百炼 Embeddings 适配器，负责把文本按百炼兼容接口要求转换为向量。

import os

from openai import OpenAI

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_embedding_model_name


class BailianEmbeddings:
    # 简要注释：初始化百炼 embeddings 客户端。
    def __init__(self):
        self.client = OpenAI(
            api_key=get_openai_api_key(),
            base_url=get_openai_base_url(),
        )
        self.model = get_embedding_model_name()

    # 简要注释：批量文本向量化，供向量库建索引使用。
    def embed_documents(self, texts):
        # 百炼 embedding API 单次最多 10 条，需要分批
        batch_size = 10
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            response = self.client.embeddings.create(
                model=self.model,
                input=batch,
                encoding_format="float",
            )
            all_embeddings.extend([item.embedding for item in response.data])
        return all_embeddings

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