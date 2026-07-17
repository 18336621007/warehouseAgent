# Graph 运行时依赖构建模块，负责统一初始化共享对象。
from agentTest.langchain_app.app_builder import build_schema_rag_app, build_langchain_tools
from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langgraph_app.runtime.graph_logger import clear_log_file
from agentTest.langgraph_app.runtime.graph_logger import get_log_file_path
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.llm import LLM


def build_graph_runtime():

    # 清空旧日志，保证每次运行都从新日志开始
    clear_log_file()

    embedding = BailianEmbeddings()
    app_context = build_schema_rag_app(embedding)
    tools = build_langchain_tools()
    llm = LLM()

    # 记录 runtime 初始化完成日志
    log_node_event("graph_runtime", f"runtime initialized, log_file={get_log_file_path()}")

    return {
        "embedding": embedding,
        "llm": llm,
        "prompt": app_context["prompt"],
        "retriever": app_context["retriever"],
        "tools": tools
    }
