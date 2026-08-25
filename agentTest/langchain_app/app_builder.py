from agentTest.langchain_app.chains.schema_rag_chain import SchemaRagChain
from agentTest.langchain_app.documents.schema_documents import SchemaDocumentsBuilder
from agentTest.langgraph_app.prompts.sql_prompts import build_sql_generation_prompt
from agentTest.langchain_app.retrievers.schema_retriever import SchemaRetriever
from agentTest.langchain_app.tools.tool_factory import build_tools
from agentTest.langchain_app.vectorstores.schema_vector_store import SchemaVectorStore
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.llm import LLM

# 向量库磁盘缓存路径
import os
_CACHE_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "langgraph_app", "cache")
_CACHE_SCHEMA_DIR = os.path.join(_CACHE_BASE, "schema_faiss_index")
_CACHE_ENRICHED_DIR = os.path.join(_CACHE_BASE, "enriched_faiss_index")

# 库级 FAISS 落盘路径
_CACHE_DB_DIR = os.path.join(_CACHE_BASE, "db_faiss_index")
# 表级 FAISS 落盘路径
_CACHE_TABLE_DIR = os.path.join(_CACHE_BASE, "table_faiss_index")
# 字段级 FAISS 落盘路径
_CACHE_COLUMN_DIR = os.path.join(_CACHE_BASE, "column_faiss_index")
# BM25 索引落盘路径
_CACHE_BM25_DIR = os.path.join(_CACHE_BASE, "bm25_index")

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
def build_langchain_tools(meta_provider=None):
    # 将共享 Metadata Provider 继续传给 Tool 工厂
    return build_tools(
        meta_provider=meta_provider,
    )


# 简要注释：创建基于增强元数据的 Schema RAG 链，用于和原始版对比。
def build_enriched_schema_rag_app(embedding, force_rebuild=False, return_stats=False):
    from agentTest.langchain_app.documents.enriched_schema_documents import EnrichedSchemaDocumentsBuilder
    from agentTest.langchain_app.retrievers.enriched_schema_retriever import EnrichedSchemaRetriever

    document_builder = EnrichedSchemaDocumentsBuilder()
    documents = document_builder.build_documents()

    # 优先从磁盘加载，不存在则从 MySQL 构建并落盘
    vector_store_manager = SchemaVectorStore(embedding)
    loaded = vector_store_manager.load_or_build(
        _CACHE_ENRICHED_DIR, documents, force_rebuild=force_rebuild, return_stats=return_stats
    )
    if return_stats:
        vector_store, sync_stats = loaded
    else:
        vector_store = loaded
        sync_stats = None

    retriever = EnrichedSchemaRetriever(vector_store)

    result = {
        "documents": documents,
        "vector_store": vector_store,
        "retriever": retriever,
    }
    if sync_stats is not None:
        result["sync_stats"] = sync_stats
    return result


# 简要注释：构建库级向量库，每库一个 Document
def build_db_rag(embedding, force_rebuild=False, return_stats=False):
    from agentTest.langchain_app.documents.enriched_db_documents import EnrichedDatabaseDocumentsBuilder

    document_builder = EnrichedDatabaseDocumentsBuilder()
    documents = document_builder.build_documents()

    vector_store_manager = SchemaVectorStore(embedding)
    loaded = vector_store_manager.load_or_build(
        _CACHE_DB_DIR, documents, force_rebuild=force_rebuild, return_stats=return_stats
    )
    if return_stats:
        vector_store, sync_stats = loaded
    else:
        vector_store = loaded
        sync_stats = None

    result = {"vector_store": vector_store, "documents": documents}
    if sync_stats is not None:
        result["sync_stats"] = sync_stats
    return result


# 简要注释：构建表级向量库，每表一个 Document
def build_table_rag(embedding, force_rebuild=False, return_stats=False):
    from agentTest.langchain_app.documents.enriched_table_documents import EnrichedTableDocumentsBuilder

    document_builder = EnrichedTableDocumentsBuilder()
    documents = document_builder.build_documents()

    vector_store_manager = SchemaVectorStore(embedding)
    loaded = vector_store_manager.load_or_build(
        _CACHE_TABLE_DIR, documents, force_rebuild=force_rebuild, return_stats=return_stats
    )
    if return_stats:
        vector_store, sync_stats = loaded
    else:
        vector_store = loaded
        sync_stats = None

    result = {"vector_store": vector_store, "documents": documents}
    if sync_stats is not None:
        result["sync_stats"] = sync_stats
    return result


# 简要注释：构建字段级向量库，每字段一个 Document
def build_column_rag(embedding, force_rebuild=False, return_stats=False):
    from agentTest.langchain_app.documents.enriched_column_documents import EnrichedColumnDocumentsBuilder

    document_builder = EnrichedColumnDocumentsBuilder()
    documents = document_builder.build_documents()

    vector_store_manager = SchemaVectorStore(embedding)
    loaded = vector_store_manager.load_or_build(
        _CACHE_COLUMN_DIR, documents, force_rebuild=force_rebuild, return_stats=return_stats
    )
    if return_stats:
        vector_store, sync_stats = loaded
    else:
        vector_store = loaded
        sync_stats = None

    result = {"vector_store": vector_store, "documents": documents}
    if sync_stats is not None:
        result["sync_stats"] = sync_stats
    return result


# 简要注释：构建 BM25 倒排索引（与 FAISS 共用 schema_documents）
def build_bm25_rag(force_rebuild=False, return_stats=False):
    """
    构建 BM25 倒排索引

    与 FAISS 向量库共用同一份 schema_documents：
    - db 层: EnrichedDatabaseDocumentsBuilder
    - table 层: EnrichedTableDocumentsBuilder
    - column 层: EnrichedColumnDocumentsBuilder
    - enriched 层: EnrichedSchemaDocumentsBuilder

    Args:
        force_rebuild: 是否强制重建（删除已有缓存）
        return_stats: 是否返回统计信息

    Returns:
        {
            "retriever": BM25Retriever 实例,
            "documents": 所有文档列表,
            "doc_sources": 各层级文档字典
        }
    """
    from agentTest.langchain_app.rag.bm25_retriever import BM25Retriever
    from agentTest.langchain_app.documents.enriched_db_documents import EnrichedDatabaseDocumentsBuilder
    from agentTest.langchain_app.documents.enriched_table_documents import EnrichedTableDocumentsBuilder
    from agentTest.langchain_app.documents.enriched_column_documents import EnrichedColumnDocumentsBuilder
    from agentTest.langchain_app.documents.enriched_schema_documents import EnrichedSchemaDocumentsBuilder

    # 文档构建器映射
    document_builders = [
        ("db", EnrichedDatabaseDocumentsBuilder),
        ("table", EnrichedTableDocumentsBuilder),
        ("column", EnrichedColumnDocumentsBuilder),
        ("enriched", EnrichedSchemaDocumentsBuilder),
    ]

    all_docs = []
    doc_sources = {}

    # 收集所有层级的文档
    for name, builder_cls in document_builders:
        builder = builder_cls()
        docs = builder.build_documents()
        # 标记来源层级
        for doc in docs:
            if not hasattr(doc, "metadata"):
                doc.metadata = {}
            doc.metadata["_bm25_source"] = name
        all_docs.extend(docs)
        doc_sources[name] = docs

    stats = {"total": len(all_docs), "sources": {}}
    for name, docs in doc_sources.items():
        stats["sources"][name] = len(docs)

    # 构建/加载 BM25 索引
    bm25_retriever = BM25Retriever()

    if force_rebuild and os.path.exists(_CACHE_BM25_DIR):
        shutil.rmtree(_CACHE_BM25_DIR)

    if os.path.exists(_CACHE_BM25_DIR) and os.listdir(_CACHE_BM25_DIR):
        bm25_retriever.load(_CACHE_BM25_DIR)
        stats["rebuilt"] = False
    else:
        bm25_retriever.build_index(all_docs)
        bm25_retriever.save(_CACHE_BM25_DIR)
        stats["rebuilt"] = True

    result = {
        "retriever": bm25_retriever,
        "documents": all_docs,
        "doc_sources": doc_sources,
    }
    if return_stats:
        result["sync_stats"] = stats

    return result
