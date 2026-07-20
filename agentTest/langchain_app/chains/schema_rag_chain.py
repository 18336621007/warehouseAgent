# 简要注释：Schema RAG 链模块，负责串联检索、提示词和模型调用。

from typing import List, Any

from agentTest.semantic.semantic_rules import match_semantic_entries, format_semantic_context


class SchemaRagChain:
    # 简要注释：初始化 RAG 链。
    def __init__(self, retriever, prompt, llm):
        self.retriever = retriever
        self.prompt = prompt
        self.llm = llm

    # 简要注释：把检索到的 document对象列表 整理为 prompt 可用的 字符串文本
    def format_schema_context(self, documents: List[Any]) -> str:
        context_list = []

        for document in documents:
            page_content = getattr(document, "page_content", None)
            if page_content is None and isinstance(document, dict):
                page_content = document.get("page_content", "")
            context_list.append(page_content or "")

        return "\n\n".join(context_list)

    # 简要注释：执行检索增强生成。
    def invoke(self, question: str):
        documents = self.retriever.retrieve(question) # 先检索相关的schema文档
        schema_context = self.format_schema_context(documents) # 把检索到的 document 整理成可以拼接给prompt的context文本

        # 匹配语义条目，生成语义指引
        matched_entries = match_semantic_entries(question)
        semantic_context = format_semantic_context(matched_entries)
        prompt_value = self.prompt.invoke({
            "question": question,
            "schema_context": schema_context,
            "semantic_context": semantic_context
        })
        return self.llm.invoke(prompt_value)

