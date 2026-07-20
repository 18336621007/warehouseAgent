from langchain_core.prompts import ChatPromptTemplate

from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState


def build_generate_sql_node(runtime):

    llm = runtime["llm"]
    default_prompt = runtime["prompt"]

    def generate_sql_node(state: AgentState) -> dict:

        question = state["question"]
        schema_context = state["schema_context"]

        retry_count = state.get("retry_count", 0)
        sql_fix_reason = state.get("sql_fix_reason", "")
        timer = start_timer()

        # 记录节点开始日志
        log_node_start(
            "generate_sql",
            retry_count=retry_count,
            question=question,
        )

        try:
            prompt = default_prompt
            prompt_input = {
                "question": question,
                "schema_context": schema_context,
            }

            # 第一轮使用标准 Prompt，修正轮补充 SQL 错误原因。
            if retry_count > 0:
                previous_sql = state.get("generated_sql", "")
                prompt = ChatPromptTemplate.from_messages([
                    (
                        "system",
                        "你是一个面向 Hive 数仓场景的 SQL 助手。请根据用户问题、schema 信息和上一次 SQL 的错误原因，重新生成更符合 Hive 语法和约束的 SQL。返回纯 SQL，不要包含解释，也不要带结尾分号。"
                    ),
                    (
                        "human",
                        "用户问题：\n{question}\n\n相关 schema 信息：\n{schema_context}\n\n上一次生成的 SQL：\n{previous_sql}\n\n所有已指出的错误原因：\n{sql_fix_reason}"
                    )
                ])
                prompt_input["previous_sql"] = previous_sql
                prompt_input["sql_fix_reason"] = sql_fix_reason

            prompt_value = prompt.invoke(prompt_input)
            generated_sql = llm.invoke(prompt_value)

            # 记录节点结束日志
            log_node_end(
                "generate_sql",
                generated_sql=generated_sql,
                schema_context_length=len(schema_context),
                duration_ms=elapsed_ms(timer),
            )
            return {
                "generated_sql": generated_sql
            }
        except Exception as error:
            # 记录节点异常日志
            log_node_error(
                "generate_sql",
                retry_count=retry_count,
                error=error,
                duration_ms=elapsed_ms(timer),
            )
            raise

    return generate_sql_node
