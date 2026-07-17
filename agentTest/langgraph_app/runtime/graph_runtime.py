# Graph 运行时依赖构建模块，负责统一初始化共享对象。
from agentTest.langchain_app.app_builder import build_schema_rag_app, build_langchain_tools
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.llm import LLM

def build_graph_runtime():
    embedding = BailianEmbeddings()
    app_context = build_schema_rag_app(embedding)
    tools = build_langchain_tools()
    llm = LLM()

    return {
        "embedding": embedding,
        "llm": llm,
        "prompt": app_context["prompt"],
        "retriever": app_context["retriever"],
        "tools": tools
    }