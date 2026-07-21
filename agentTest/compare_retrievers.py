import sys
sys.path.insert(0, r"D:\code\Project\test")
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.app_builder import build_enriched_schema_rag_app

embedding = BailianEmbeddings()
enriched = build_enriched_schema_rag_app(embedding)

question = "最近7天补贴了多少金额"

print("=== 增强 Schema 检索 ===")
for doc in enriched["retriever"].retrieve(question):
    meta = doc.metadata
    name = meta.get("table_name", "?")
    print(f"  [{name}]")
    print(f"  {doc.page_content}")
    print()
