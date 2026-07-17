from agentTest.langchain_app.prompts.sql_generation_prompt import build_sql_generation_prompt
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.llm import LLM

def build_generate_sql_node(runtime):
    llm = runtime["llm"]
    prompt = runtime["prompt"]

    def generate_sql_node(state: AgentState) -> dict:
        question = state["question"]
        schema_context = state["schema_context"]

        prompt_value = prompt.invoke(
            {
                "question": question,
                "schema_context": schema_context,
            }
        )

        generated_sql = llm.invoke(prompt_value)


        return {
            "generated_sql": generated_sql
        }

    return generate_sql_node