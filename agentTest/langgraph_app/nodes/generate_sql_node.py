from langchain_core.prompts import ChatPromptTemplate

from agentTest.langchain_app.utils.sql_cleaner import clear_sql
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState

MAX_CONSISTENCY_RETRIES = 2  # 方案一致性校验最多重试次数


def _check_plan_consistency(sql: str, confirmed_plan: dict, advisor_last_answer: str, llm) -> str:
    """用 LLM 检查生成的 SQL 是否忠实实现了确认方案。
    返回不一致原因字符串，一致时返回空字符串。"""
    tables = confirmed_plan.get("tables", [])
    fields = confirmed_plan.get("fields", [])

    check_prompt = ChatPromptTemplate.from_messages([
        ("system", """你是一个 SQL 审计助手。请对比「已确认的分析方案」和「生成的 SQL」，判断 SQL 是否忠实实现了方案。

检查要点：
- SQL 是否使用了方案中指定的表和字段
- SQL 的时间分区条件是否与方案描述一致（如"昨天"应对应 date_sub，不能写成字符串字面量）
- SQL 的过滤条件是否覆盖了方案中提到的筛选条件
- SQL 的聚合方式是否符合方案中描述的口径

返回格式：
- 如果一致，只返回一个词：PASS
- 如果不一致，一句话说明哪里不一致（中文），用于修正 SQL。不要输出 SQL，只说明问题。
"""),
        ("human", """已确认的分析方案：
- 数据表: {tables}
- 字段: {fields}
- Advisor 对方案的描述（含时间条件、过滤条件等）：
{advisor_answer}

生成的 SQL：
{sql}

请判断 SQL 是否忠实实现了上述方案。"""),
    ])

    prompt_value = check_prompt.invoke({
        "tables": ", ".join(tables),
        "fields": ", ".join(fields),
        "advisor_answer": advisor_last_answer[:1200],
        "sql": sql,
    })
    result = llm.invoke(prompt_value)
    content = result.content.strip() if hasattr(result, 'content') else str(result).strip()

    if content.upper().startswith("PASS"):
        return ""
    return content


def build_generate_sql_node(runtime):
    llm = runtime["llm"]
    default_prompt = runtime["prompt"]

    def generate_sql_node(state: AgentState) -> dict:
        # ── 读取独立的 confirmed_plan（Advisor 写入），格式化为 prompt 独立 section ──
        confirmed_plan = state.get("confirmed_plan") or {}
        confirmed_section = ""
        if confirmed_plan.get("tables"):
            tables = confirmed_plan.get("tables", [])
            fields = confirmed_plan.get("fields", [])
            confirmed_section = (
                "【已确认的分析方案 — 必须使用以下表和字段】\n"
                f"- 数据表: {', '.join(tables)}\n"
                f"- 字段: {', '.join(fields)}"
            )

        question = state["question"]
        schema_context = state["schema_context"]
        advisor_last_answer = state.get("advisor_last_answer", "")

        retry_count = state.get("retry_count", 0)
        sql_fix_reason = state.get("sql_fix_reason", "")
        timer = start_timer()

        log_node_start("generate_sql", retry=retry_count, question=question)

        try:
            prompt = default_prompt
            prompt_input = {
                "question": question,
                "schema_context": schema_context,
                "confirmed_section": confirmed_section
            }

            if retry_count > 0:
                if confirmed_section:
                    prompt_input["schema_context"] = confirmed_section + "\n\n" + schema_context

                previous_sql = state.get("generated_sql", "")
                prompt = ChatPromptTemplate.from_messages([
                    ("system", "你是一个面向 Hive 数仓场景的 SQL 助手。请根据用户问题、schema 信息和上一次 SQL 的错误原因，重新生成更符合 Hive 语法和约束的 SQL。返回纯 SQL，不要包含解释，也不要带结尾分号。"),
                    ("human", "用户问题：\n{question}\n\n相关 schema 信息：\n{schema_context}\n\n上一次生成的 SQL：\n{previous_sql}\n\n所有已指出的错误原因：\n{sql_fix_reason}")
                ])
                prompt_input["previous_sql"] = previous_sql
                prompt_input["sql_fix_reason"] = sql_fix_reason

            prompt_value = prompt.invoke(prompt_input)
            generated_sql = llm.invoke(prompt_value)
            generated_sql = clear_sql(generated_sql)

            # ── 方案一致性校验：确认 SQL 忠实实现了 confirmed_plan ──
            consistency_retry = 0
            while confirmed_plan.get("tables") and consistency_retry < MAX_CONSISTENCY_RETRIES:
                inconsistency = _check_plan_consistency(
                    generated_sql, confirmed_plan, advisor_last_answer, llm
                )
                if not inconsistency:
                    break  # 一致，通过

                # 不一致 → 带原因重新生成
                consistency_retry += 1
                retry_count += 1
                log_node_start("generate_sql", retry=retry_count, consistency_fix=inconsistency[:80])

                fix_prompt = ChatPromptTemplate.from_messages([
                    ("system", "你是一个面向 Hive 数仓场景的 SQL 助手。请根据用户问题、schema 信息和方案不一致的原因，重新生成 SQL。返回纯 SQL，不要包含解释，也不要带结尾分号。"),
                    ("human", "用户问题：\n{question}\n\n已确认的方案信息：\n{confirmed_section}\n\n相关 schema 信息：\n{schema_context}\n\n方案不一致的原因：\n{inconsistency}\n\n上次生成的 SQL：\n{previous_sql}")
                ])
                fix_input = {
                    "question": question,
                    "confirmed_section": confirmed_section,
                    "schema_context": schema_context,
                    "inconsistency": inconsistency,
                    "previous_sql": generated_sql,
                }
                generated_sql = clear_sql(llm.invoke(fix_prompt.invoke(fix_input)))

            log_node_end(
                "generate_sql",
                sql=str(generated_sql),
                ctx_len=len(schema_context),
                ms=elapsed_ms(timer),
            )
            return {"generated_sql": generated_sql}
        except Exception as error:
            log_node_error("generate_sql", retry=retry_count, error=str(error), ms=elapsed_ms(timer))
            raise

    return generate_sql_node
