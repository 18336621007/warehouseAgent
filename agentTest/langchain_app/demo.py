from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.app_builder import build_schema_rag_app, build_langchain_tools


# 简要注释：运行最小演示流程。
def run_demo(embeddings, question: str):
    app_context = build_schema_rag_app(embeddings)
    tools = build_langchain_tools()

    chain = app_context["chain"]
    result = chain.invoke(question)

    return {
        "question": question,
        "result": result,
        "tool_names": [tool.name for tool in tools]
    }


# 简要注释：本地示例入口，初始化百炼 embeddings 后执行演示。
if __name__ == "__main__":
    embeddings = BailianEmbeddings()
    question = "查询最近7天订单量最高的商品"

    demo_result = run_demo(embeddings, question)
    print(demo_result)















