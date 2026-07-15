class SchemaGroundingService:
    # SchemaGroundingService 负责为当前 query 准备 schema grounding 信息：
    # - 生成结构化 schema_context
    # - 生成文档型 schema_rag_context
    # 上层 Agent 只关心最终产物，不再关心内部构建细节。

    def __init__(self, schema_context_builder, schema_snapshot_service, schema_vector_retriever):
        # 输入：
        # - schema_context_builder：构造结构化 schema_context 的组件
        # - schema_snapshot_service：构造 schema documents 快照的组件
        # - schema_vector_retriever：从 documents 中检索相关 schema 文档的组件
        # 输出：
        # - 无直接输出，prepare 方法返回 schema grounding 结果
        self.schema_context_builder = schema_context_builder
        self.schema_snapshot_service = schema_snapshot_service
        self.schema_vector_retriever = schema_vector_retriever

    def build_schema_rag_context(self, query: str):
        # 输入：
        # - query：用户问题
        # 输出：
        # - retrieved_documents：最相关的 schema documents 列表
        schema_documents = self.schema_snapshot_service.build_snapshot()
        retrieved_documents = self.schema_vector_retriever.retrieve(query, schema_documents, top_k=3)
        return retrieved_documents

    def prepare(self, query: str):
        # 输入：
        # - query：用户问题
        # 输出：
        # - schema_context：结构化候选表/字段上下文
        # - schema_rag_context：文档型 schema 检索上下文
        schema_context = self.schema_context_builder.build(query)
        schema_rag_context = self.build_schema_rag_context(query)
        return schema_context, schema_rag_context
