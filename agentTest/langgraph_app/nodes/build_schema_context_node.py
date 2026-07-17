from agentTest.langchain_app.chains.schema_rag_chain import SchemaRagChain
from agentTest.langgraph_app.state.agent_state import AgentState

# Schema 上下文构建节点，负责把检索文档整理成 prompt 可用文本。

def build_schema_context_node(state: AgentState) -> dict:
    schema_documents = state.get("schema_documents", [])

    temp_chain = SchemaRagChain(
        retriever=None,
        prompt=None,
        llm=None
    )

    schema_context = temp_chain.format_schema_context(schema_documents)

    return {
        "schema_context": schema_context
    }
