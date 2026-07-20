# Schema 上下文增强节点，负责结合 schema tool 补充精确表结构信息。
from agentTest.langchain_app.chains.schema_rag_chain import SchemaRagChain
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.semantic.semantic_rules import match_semantic_entries, format_semantic_context


def build_enrich_schema_context_node(runtime):
    tools = runtime["tools"]
    describe_table_tool = next(tool for tool in tools if tool.name == "describe_table")
    def enrich_schema_context_node(state: AgentState):
        schema_documents = state.get("schema_documents", [])

        #复用chain里的format_schema_context，把documents 转为 context
        temp_chain = SchemaRagChain(
            retriever=None,
            prompt=None,
            llm=None,
        )
        schema_context = temp_chain.format_schema_context(schema_documents)  #"表名: ods_order\n表说明: 订单表\n字段信息:\n- order_id | string | 订单id",

        # 从首个检索文档中尝试提取表名，并补充精确表结构。
        if schema_documents:
            first_doc = schema_documents[0]
            table_name = getattr(first_doc, "metadata", {}).get("table_name", "")

            if table_name:
                table_schema = describe_table_tool.invoke({
                    "table_name": table_name,
                })
                schema_context = (
                    schema_context
                    + "\n\n"
                    +"【精确表结构补充】\n"
                    +str(table_schema)
                )
                # "database_name": self.config["database"],
                # "table_name": table_name,
                # "table_comment": "",
                # "table_type": "",
                # "columns": columns,

        # 匹配语义条目，拼接到上下文开头，让LLM优先参考
        question = state["question"]
        matched_entries = match_semantic_entries(question)
        if matched_entries:
            semantic_context = format_semantic_context(matched_entries)
            schema_context = semantic_context + "\n\n" + schema_context


        return {
            "schema_context": schema_context
        }
    return enrich_schema_context_node