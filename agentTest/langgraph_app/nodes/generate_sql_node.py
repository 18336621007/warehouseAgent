from langchain_core.prompts import ChatPromptTemplate

from agentTest.langchain_app.prompts.sql_generation_prompt import build_sql_generation_prompt
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.llm import LLM

def build_generate_sql_node(runtime):

    llm = runtime["llm"]
    default_prompt = runtime["prompt"]
    def generate_sql_node(state: AgentState) -> dict:

        question = state["question"]
        schema_context = state["schema_context"]

        retry_count = state.get("retry_count", 0)
        sql_fix_reason = state.get("sql_fix_reason", "")

        log_node_event("generate_sql", f"开始生成SQL，retry_count={retry_count}")

        prompt = default_prompt
        prompt_input = {
            "question": question,
            "schema_context": schema_context,
        }

        # 第一次生成走标准SQL生成Prompt，修正轮次补充错误原因
        if retry_count > 0:
            prompt = ChatPromptTemplate.from_messages([
                (
                    "system",
                    "你是一个面向 Hive 数仓场景的 SQL 助手。请根据用户问题、schema 信息和上一次 SQL 的错误原因，重新生成更符合 Hive 语法和约束的 SQL。返回纯 SQL，不要包含解释，也不要带结尾分号。"
                ),
                (
                    "human",
                    "用户问题：\n{question}\n\n相关 schema 信息：\n{schema_context}\n\n上一次 SQL 的错误原因：\n{sql_fix_reason}"
                )

            ])
            prompt_input["sql_fix_reason"] = sql_fix_reason


        prompt_value = prompt.invoke(prompt_input)
        generated_sql = llm.invoke(prompt_value)

        log_node_event("generate_sql", f"SQL生成完成，sql={generated_sql}")
        return {
            "generated_sql": generated_sql
        }

    return generate_sql_node