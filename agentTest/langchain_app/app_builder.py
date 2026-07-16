from agentTest.langchain_app.chains.schema_rag_chain import SchemaRagChain
from agentTest.langchain_app.documents.schema_documents import SchemaDocumentsBuilder
from agentTest.langchain_app.prompts.sql_generation_prompt import build_sql_generation_prompt
from agentTest.langchain_app.retrievers.schema_retriever import SchemaRetriever
from agentTest.langchain_app.tools.tool_factory import build_tools
from agentTest.langchain_app.vectorstores.schema_vector_store import SchemaVectorStore
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.llm import LLM


# 简要注释：创建 Schema RAG 链并统一返回相关对象。
def build_schema_rag_app(embedding):
    meta_provider = HiveMetadataProvider()
    llm = LLM()

    document_builder = SchemaDocumentsBuilder(meta_provider)
    documents = document_builder.build_documents()

    vector_store = SchemaVectorStore(embedding).build(documents)
    retriever = SchemaRetriever(vector_store)
    prompt = build_sql_generation_prompt()

    chain = SchemaRagChain(
        retriever=retriever,
        prompt=prompt,
        llm=llm,
    )

    return {
        "llm": llm,
        "documents": documents,
        "vector_store": vector_store,
        "retriever": retriever,
        "prompt": prompt,
        "chain": chain,
    }


# 简要注释：构建当前项目可用的标准 tools 列表。
def build_langchain_tools():
    return build_tools()
