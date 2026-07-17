# Schema 检索节点，负责根据用户问题召回相关 schema 文档。
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.state.agent_state import AgentState


def build_retrieve_schema_node(runtime):

    retriever = runtime["retriever"]

    def retrieve_schema_node(state: AgentState) -> dict:

        question = state["question"]

        # 打印节点开始日志
        log_node_start("retrieve_schema", question=question)

        schema_documents = retriever.retrieve(question)

        # 打印节点结束日志
        log_node_end("retrieve_schema", documents=len(schema_documents))

        return {
            "schema_documents": schema_documents
        }


    return retrieve_schema_node
