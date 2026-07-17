from langchain_core.prompts import ChatPromptTemplate

from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.llm import LLM


def build_final_answer_node(state: AgentState):
    llm = LLM()
    sql_result = state.get("sql_result", "")
    question = state.get("question", "")
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
        "sql_result": sql_result,
        "question": question,
    })

    final_answer = llm.invoke(prompt_value)

    return {
        "final_answer": final_answer,
    }


