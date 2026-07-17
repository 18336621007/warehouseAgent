from agentTest.langchain_app.prompts.sql_generation_prompt import build_sql_generation_prompt
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.llm import LLM

def generate_sql_node(state: AgentState) -> dict:
    question = state["question"]
    schema_context = state.get("schema_context", "")

    prompt = build_sql_generation_prompt()
    llm = LLM()

    prompt_value = prompt.invoke(
        {
            "question": question,
            "schema_context": schema_context,
        }
    )

    generation_sql = llm.invoke(prompt_value)


    return {
        "generation_sql": generation_sql
    }