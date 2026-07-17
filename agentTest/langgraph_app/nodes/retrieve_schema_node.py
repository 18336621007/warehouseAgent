# Schema 检索节点，负责根据用户问题召回相关 schema 文档。
from agentTest.langchain_app.app_builder import build_schema_rag_app
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langgraph_app.state.agent_state import AgentState


def retrieve_schema_node(state: AgentState):
    embeddings = BailianEmbeddings()
    app_context = build_schema_rag_app(embeddings)

    retriever = app_context["retriever"]
    question = app_context["question"]

    schema_documents = retriever.retrieve(question)

    return {
        "schema_documents": schema_documents
    }