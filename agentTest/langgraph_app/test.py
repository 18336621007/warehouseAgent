
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.app_builder import build_enriched_schema_rag_app

embedding = BailianEmbeddings()
result = build_enriched_schema_rag_app(embedding)
print('向量库已落地到 cache/enriched_faiss_index/')
