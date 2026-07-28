# generate_sql_node.py —— SQL 生成节点
# 新增 SQL 级全量校验：以 confirmed_plan 为基准，校验表名/度量/维度/时间/过滤条件
# 新增降级 SQL 构造：LLM 重试仍不一致时，根据 confirmed_plan 直接构造标准 SQL
import re
from langchain_core.prompts import ChatPromptTemplate

from agentTest.langchain_app.utils.sql_cleaner import clear_sql
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_error
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.state.agent_state import AgentState

MAX_CONSISTENCY_RETRIES = 2  # 方案一致性校验最多重试次数

def _format_examples(docs: list) -> str:
    """将检索到的历史优质示例转为 Few-shot 文本，LLM 自行从完整 SQL 中学模式"""
    if not docs:
        return ""
    lines = ["【历史相似查询示例 —— 请重点关注 SQL 中的聚合方式、GROUP BY 维度、日期处理】"]
    for i, doc in enumerate(docs, 1):
        q = doc.metadata.get("question", "")
        s = doc.metadata.get("sql", "")
        tables = doc.metadata.get("tables", "[]")
        lines.append(f"\n示例{i}：")
        lines.append(f"  问题：{q}")
        lines.append(f"  表：{tables}")
        lines.append(f"  SQL：{s}")
    return "\n".join(lines)



# ── 标准日期处理函数映射 ──
DATE_FORMAT_HIVE_EXPR = {
    "yyyyMMdd": "regexp_replace(date_sub(current_date(), 1), '-', '')",
    "yyyy-MM-dd": "date_sub(current_date(), 1)",
}


def _build_fallback_sql(confirmed_plan: dict) -> str:
    tables = confirmed_plan.get("tables") or []
    table = confirmed_plan.get("table", "") or (tables[0] if tables else "")
    measures = confirmed_plan.get("measures", [])
    dimensions = confirmed_plan.get("dimensions", [])
    time_field = confirmed_plan.get("time_field", "pt_dt")
    filters = confirmed_plan.get("filters", "")

    if not table or not measures:
        return ""

    select_parts = list(dimensions)
    for m in measures:
        select_parts.append(f"SUM({m}) AS {m}")

    date_expr = DATE_FORMAT_HIVE_EXPR.get("yyyyMMdd", "date_sub(current_date(), 1)")

    where_parts = [f"{time_field} = {date_expr}"]
    if filters and filters.strip():
        where_parts.append(filters.strip())

    sql_lines = [
        f"SELECT {', '.join(select_parts)}",
        f"FROM {table}",
        f"WHERE {' AND '.join(where_parts)}",
        f"GROUP BY {', '.join(dimensions)}",
        "LIMIT 1000",
    ]
    return "\n".join(sql_lines)


def _validate_sql_against_plan(sql: str, confirmed_plan: dict) -> list:
    issues = []
    sql_upper = sql.upper()
    tables = confirmed_plan.get("tables") or []
    table = confirmed_plan.get("table", "") or (tables[0] if tables else "")
    measures = confirmed_plan.get("measures", [])
    dimensions = confirmed_plan.get("dimensions", [])
    time_field = confirmed_plan.get("time_field", "pt_dt")
    time_range = confirmed_plan.get("time_range", "") or "昨天"  # 未指定默认昨天（企业惯例：当天分区常为空）
    filters = confirmed_plan.get("filters", "")

    if not table:
        return issues

    # 1. 表名校验
    table_normalized = table.replace(".", "\\.").lower()
    if not re.search(table_normalized, sql.lower()):
        issues.append(f"SQL 中未找到方案指定的表: {table}")

    # 2. 度量字段校验
    for m in measures:
        agg_pattern = r"(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*" + re.escape(m) + r"\s*\)"
        if not re.search(agg_pattern, sql, re.IGNORECASE):
            issues.append(f"度量字段 {m} 未使用聚合函数（需要 SUM/COUNT/AVG/MAX/MIN 包裹）")

    # 3. 维度字段校验
    has_group_by = "GROUP BY" in sql_upper
    for d in dimensions:
        if d.lower() not in sql.lower():
            issues.append(f"维度字段 {d} 未出现在 SQL 中")
        elif has_group_by:
            group_part = sql_upper.split("GROUP BY")[1]
            if "ORDER BY" in group_part:
                group_part = group_part.split("ORDER BY")[0]
            if "LIMIT" in group_part:
                group_part = group_part.split("LIMIT")[0]
            if d.lower() not in group_part.lower():
                issues.append(f"维度字段 {d} 在 SELECT 中但不在 GROUP BY 中")

    # 4. 时间分区字段校验
    if time_field.lower() not in sql.lower():
        issues.append(f"时间分区字段 {time_field} 未出现在 SQL 中")
    elif "WHERE" in sql_upper:
        where_part = sql_upper.split("WHERE")[1]
        for keyword in ["GROUP BY", "ORDER BY", "LIMIT"]:
            if keyword in where_part:
                where_part = where_part.split(keyword)[0]
        if time_field.lower() not in where_part.lower():
            issues.append(f"时间分区字段 {time_field} 不在 WHERE 条件中")

    # 5. 日期值校验：time_range说"昨天"但SQL没用date_sub → 报错
    if time_range:
        sql_lower = sql.lower()
        if "昨天" in time_range and "date_sub" not in sql_lower:
            issues.append(f"时间条件应为昨天(date_sub(current_date(),1))，但SQL中未找到date_sub，疑似使用了当天日期")
        elif "今天" in time_range and "current_date" not in sql_lower:
            issues.append(f"时间条件应为今天(current_date())，但SQL中未找到current_date")

    # 6. filters 校验
    if filters and filters.strip():
        filter_normalized = filters.strip().lower().replace(" ", "")
        sql_normalized = sql.lower().replace(" ", "")
        if filter_normalized not in sql_normalized:
            issues.append(f"过滤条件 '{filters}' 未出现在 SQL 的 WHERE 中")

    return issues


def _check_plan_consistency(sql: str, confirmed_plan: dict, advisor_last_answer: str, llm) -> str:
    tables = confirmed_plan.get("tables", [])
    fields = confirmed_plan.get("fields", [])
    measures = confirmed_plan.get("measures", []) or confirmed_plan.get("fields", [])
    dimensions = confirmed_plan.get("dimensions", [])
    time_field = confirmed_plan.get("time_field", "pt_dt")
    filters = confirmed_plan.get("filters", "")

    # ── 先做程序化校验 ──
    programmatic_issues = _validate_sql_against_plan(sql, confirmed_plan)
    if programmatic_issues:
        return "; ".join(programmatic_issues)

    # ── 程序化校验通过，再用 LLM 做语义校验 ──
    check_prompt = ChatPromptTemplate.from_messages([
        ("system", """你是一个 SQL 审计助手。请对比已确认的分析方案和生成的 SQL，判断 SQL 是否忠实实现了方案。

检查要点：
- SQL 是否使用了方案中指定的表和字段
- SQL 的时间分区条件是否与方案描述一致
- SQL 的过滤条件是否覆盖了方案中提到的筛选条件
- SQL 的聚合方式是否符合方案描述

返回格式：
- 如果一致，只返回一个词：PASS
- 如果不一致，一句话说明哪里不一致（中文），不要输出 SQL。
"""),
        ("human", """已确认的分析方案：
- 数据表: {table}
- 度量字段: {measures}
- 维度字段: {dimensions}
- 时间分区: {time_field}
- 额外过滤: {filters}
- Advisor 对方案的描述：
{advisor_answer}

生成的 SQL：
{sql}

请判断 SQL 是否忠实实现了上述方案。"""),
    ])

    prompt_value = check_prompt.invoke({
        "table": ", ".join(tables),
        "measures": ", ".join(measures) if measures else "无（仅查询维度信息）",
        "dimensions": ", ".join(dimensions) if dimensions else "无",
        "time_field": time_field,
        "filters": filters or "无",
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
        confirmed_plan = state.get("confirmed_plan") or {}
        # 企业级规则：走到 generate_sql 的请求必须有 confirmed_plan
        if not confirmed_plan.get("table") and not confirmed_plan.get("tables"):
            log_node_error("generate_sql", error="缺少confirmed_plan，无法生成SQL（应在此之前由Planner兜底路由Advisor）")
            return {"generated_sql": "", "sql_error": "缺少已确认的分析方案"}
        confirmed_section = ""
        if confirmed_plan.get("table") or confirmed_plan.get("tables"):
            tables = confirmed_plan.get("tables") or []
            table = confirmed_plan.get("table", "") or (tables[0] if tables else "")
            measures = confirmed_plan.get("measures", [])
            dimensions = confirmed_plan.get("dimensions", [])
            time_field = confirmed_plan.get("time_field", "pt_dt")
            filters = confirmed_plan.get("filters", "")

            parts = [f"- 数据表: {table}"]
            if measures:
                parts.append(f"- 度量字段（必须用聚合函数 SUM/COUNT/AVG）: {', '.join(measures)}")
            if dimensions:
                parts.append(f"- 维度字段（必须在 SELECT 和 GROUP BY 中）: {', '.join(dimensions)}")
            time_range_cs = confirmed_plan.get("time_range", "") or "昨天"
            parts.append(f"- 时间分区（必须在 WHERE 中）: {time_field}（{time_range_cs}）")
            if filters:
                parts.append(f"- 额外过滤条件（必须在 WHERE 中）: {filters}")

            confirmed_section = "【已确认的分析方案 —— 以下规则必须严格遵守】\n" + "\n".join(parts)

        question = state["question"]
        schema_context = state["schema_context"]
        advisor_last_answer = state.get("advisor_last_answer", "")

        retry_count = state.get("retry_count", 0)
        sql_fix_reason = state.get("sql_fix_reason", "")
        timer = start_timer()

        log_node_start("generate_sql", retry=retry_count, question=question)

        try:
            prompt = default_prompt

            # ── 新增：检索历史优质示例作为 Few-shot ──
            example_vs = runtime.get("example_vector_store")
            example_section = ""
            if example_vs:
                similar_examples = example_vs.search_similar(
                    question,
                    current_tables=confirmed_plan.get("tables", []),
                    k=2
                )
                example_section = _format_examples(similar_examples)
                log_node_event("generate_sql", f"优秀示例检索: 命中{len(similar_examples)}条")
            prompt_input = {
                "question": question,
                "schema_context": schema_context,
                "confirmed_section": confirmed_section,
                "example_section": example_section
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

            # ── 方案一致性校验 ──
            consistency_retry = 0
            if confirmed_plan.get("table") or confirmed_plan.get("tables"):
                while consistency_retry < MAX_CONSISTENCY_RETRIES:
                    inconsistency = _check_plan_consistency(
                        generated_sql, confirmed_plan, advisor_last_answer, llm
                    )
                    if not inconsistency:
                        break

                    consistency_retry += 1
                    retry_count += 1
                    log_node_start("generate_sql", retry=retry_count,
                                   consistency_fix=inconsistency[:80])

                    fix_prompt = ChatPromptTemplate.from_messages([
                        ("system", "你是一个面向 Hive 数仓场景的 SQL 助手。请根据用户问题、schema 信息和方案不一致的原因，重新生成 SQL。返回纯 SQL，不要包含解释，也不要带结尾分号。"),
                        ("human", "用户问题：\n{question}\n\n已确认的方案信息：\n{confirmed_section}\n\n相关 schema 信息：\n{schema_context}\n\n方案不一致的原因：\n{inconsistency}\n\n上次生成的 SQL：\n{previous_sql}")
                    ])
                    fix_input = {
                        "question": question,
                        "confirmed_section": confirmed_section,
                "example_section": example_section,
                        "schema_context": schema_context,
                        "inconsistency": inconsistency,
                        "previous_sql": generated_sql,
                    }
                    generated_sql = clear_sql(llm.invoke(fix_prompt.invoke(fix_input)))

                # ── 降级：重试耗尽 → 直接构造标准 SQL ──
                if consistency_retry >= MAX_CONSISTENCY_RETRIES:
                    remaining_issues = _validate_sql_against_plan(generated_sql, confirmed_plan)
                    if remaining_issues:
                        fallback_sql = _build_fallback_sql(confirmed_plan)
                        if fallback_sql:
                            log_node_event("generate_sql",
                                f"降级 SQL 构造: LLM 重试 {MAX_CONSISTENCY_RETRIES} 次仍不一致，"
                                f"使用 confirmed_plan 直接构造。剩余问题: {remaining_issues}")
                            generated_sql = fallback_sql

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
