from langchain_core.prompts import ChatPromptTemplate

from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.llm import LLM

def build_build_final_answer_node(runtime):
    llm = runtime["llm"]
    def build_final_answer_node(state: AgentState):

        sql_valid = state.get("sql_valid", False)
        question = state.get("question", "")

        # sql校验失败返回error
        if not sql_valid:
            sql_error = state.get("sql_error", "SQL校验失败")

            return {
                "final_answer": f"本次未执行 SQL 查询，因为生成的 SQL 未通过校验。原因：{sql_error}"
            }

        # sql校验成功
        sql_result = state.get("sql_result", {})
        row_count = sql_result.get("row_count", 0) if isinstance(sql_result, dict) else 0

        if row_count == 0:

            return {
                "final_answer": "SQL 已成功执行，但没有查询到符合条件的数据。"
            }


        prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                "你是一个数据分析助手。请严格基于提供的 SQL 查询结果回答用户问题，不要编造信息。"
            ),
            (
                "human",
                "用户问题：\n{question}\n\nSQL 执行结果：\n{sql_result}"
            )
        ])

        prompt_value = prompt.invoke({
            "question": question,
            "sql_result": sql_result,
        })

        final_answer = llm.invoke(prompt_value)

        return {
            "final_answer": final_answer,
        }


    return build_final_answer_node


