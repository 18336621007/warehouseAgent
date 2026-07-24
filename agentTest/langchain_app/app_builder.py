from agentTest.langchain_app.chains.schema_rag_chain import SchemaRagChain
from agentTest.langchain_app.documents.schema_documents import SchemaDocumentsBuilder
from agentTest.langgraph_app.prompts.sql_generation_prompt import build_sql_generation_prompt
from agentTest.langchain_app.retrievers.schema_retriever import SchemaRetriever
from agentTest.langchain_app.tools.tool_factory import build_tools
from agentTest.langchain_app.vectorstores.schema_vector_store import SchemaVectorStore
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.llm import LLM

# 向量库磁盘缓存路径
_CACHE_SCHEMA_DIR = "cache/schema_faiss_index"
_CACHE_ENRICHED_DIR = "cache/enriched_faiss_index"

# 库级 FAISS 落盘路径
_CACHE_DB_DIR = "cache/db_faiss_index"
# 表级 FAISS 落盘路径
_CACHE_TABLE_DIR = "cache/table_faiss_index"
# 字段级 FAISS 落盘路径
_CACHE_COLUMN_DIR = "cache/column_faiss_index"

# 简要注释：创建 Schema RAG 链并统一返回相关对象（原始版）。
def build_schema_rag_app(embedding):
    meta_provider = HiveMetadataProvider()
    llm = LLM()

    document_builder = SchemaDocumentsBuilder(meta_provider)
    documents = document_builder.build_documents()

    # 优先从磁盘加载向量库，不存在则构建并落盘
    vector_store_manager = SchemaVectorStore(embedding)
    vector_store = vector_store_manager.load_or_build(_CACHE_SCHEMA_DIR, documents)

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


# 简要注释：创建基于增强元数据的 Schema RAG 链，用于和原始版对比。
def build_enriched_schema_rag_app(embedding):
    from agentTest.langchain_app.documents.enriched_schema_documents import EnrichedSchemaDocumentsBuilder
    from agentTest.langchain_app.retrievers.enriched_schema_retriever import EnrichedSchemaRetriever

    document_builder = EnrichedSchemaDocumentsBuilder()
    documents = document_builder.build_documents()

    # 优先从磁盘加载，不存在则从 MySQL 构建并落盘
    vector_store_manager = SchemaVectorStore(embedding)
    vector_store = vector_store_manager.load_or_build(_CACHE_ENRICHED_DIR, documents)

    retriever = EnrichedSchemaRetriever(vector_store)

    return {
        "documents": documents,
        "vector_store": vector_store,
        "retriever": retriever,
    }


# 简要注释：构建库级向量库，每库一个 Document
def build_db_rag(embedding):
    from agentTest.langchain_app.documents.enriched_db_documents import EnrichedDatabaseDocumentsBuilder

    document_builder = EnrichedDatabaseDocumentsBuilder()
    documents = document_builder.build_documents()

    vector_store_manager = SchemaVectorStore(embedding)
    vector_store = vector_store_manager.load_or_build(_CACHE_DB_DIR, documents)

    return {"vector_store": vector_store, "documents": documents}


# 简要注释：构建表级向量库，每表一个 Document
def build_table_rag(embedding):
    from agentTest.langchain_app.documents.enriched_table_documents import EnrichedTableDocumentsBuilder

    document_builder = EnrichedTableDocumentsBuilder()
    documents = document_builder.build_documents()

    vector_store_manager = SchemaVectorStore(embedding)
    vector_store = vector_store_manager.load_or_build(_CACHE_TABLE_DIR, documents)

    return {"vector_store": vector_store, "documents": documents}


# 简要注释：构建字段级向量库，每字段一个 Document
def build_column_rag(embedding):
    from agentTest.langchain_app.documents.enriched_column_documents import EnrichedColumnDocumentsBuilder

    document_builder = EnrichedColumnDocumentsBuilder()
    documents = document_builder.build_documents()

    vector_store_manager = SchemaVectorStore(embedding)
    vector_store = vector_store_manager.load_or_build(_CACHE_COLUMN_DIR, documents)

    return {"vector_store": vector_store, "documents": documents}