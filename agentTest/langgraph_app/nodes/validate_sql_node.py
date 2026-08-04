# 作用：校验 Hive SQL 是否满足安全规则
# 输入：SQL 字符串
# 输出：(bool, str)
# 第一个值表示是否通过
# 第二个值表示提示信息
from agentTest.db.hive_guardrails import validate_sql_with_guardrails
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, log_node_event, log_node_start
from agentTest.langgraph_app.state.agent_state import AgentState
import re
from agentTest.validate.sql_validate import validate_hive_sql
from agentTest.langgraph_app.services.sql_safety_validator import validate_sql_safety_simple
from agentTest.langgraph_app.services.sql_table_filter_validator import validate_table_plan_filters


def _normalize_join_keys(keys) -> list[str]:
    """统一单字段和复合字段Join键格式。"""
    if isinstance(keys, str):
        return [keys]
    return [key for key in (keys or []) if isinstance(key, str) and key]


def _validate_multi_table(sql: str, confirmed_plan: dict) -> list[str]:
    """多表场景下的额外安全校验：表完整性、笛卡尔积检测、JOIN键匹配"""
    issues = []
    tables = confirmed_plan.get("tables") or []
    joins = confirmed_plan.get("joins") or []

    if len(tables) <= 1 and not joins:
        return issues

    sql_upper = sql.upper()

    # 1. 禁止逗号分隔的多表（笛卡尔积风险）
    # 匹配 FROM table1, table2 或 FROM table1 t1, table2 t2
    from_match = re.search(
        r'FROM\s+(.+?)(?:WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|$|\))',
        sql_upper, re.DOTALL
    )
    if from_match:
        from_clause = from_match.group(1)
        if ',' in from_clause and 'JOIN' not in from_clause:
            issues.append("禁止使用逗号分隔多表（笛卡尔积风险），请使用明确的 JOIN 语法")

    # 2. 所有方案表必须出现在 SQL 中
    for t in tables:
        t_normalized = t.replace(".", "\\.").lower()
        if not re.search(t_normalized, sql.lower()):
            issues.append(f"方案表 {t} 未出现在 SQL 中")

    # 3. JOIN 键匹配（针对每条约定的 JOIN 边）
    if joins:
        if 'JOIN' not in sql_upper:
            issues.append("方案要求多表 JOIN 但 SQL 中未包含 JOIN 关键字")
        else:
            # 提取每个完整ON片段，复合键中的AND不能作为片段结束标记。
            on_clauses = re.findall(
                r'\bON\s+(.+?)(?=\b(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|$)',
                sql_upper,
                re.DOTALL,
            )
            for edge in joins:
                left_keys = _normalize_join_keys(edge["left_key"])
                right_keys = _normalize_join_keys(edge["right_key"])
                if len(left_keys) != len(right_keys) or not left_keys:
                    issues.append("JOIN关系配置中的左右键数量不一致")
                    continue

                matched = any(
                    all(
                        left_key.upper() in on_clause and right_key.upper() in on_clause
                        for left_key, right_key in zip(left_keys, right_keys)
                    )
                    for on_clause in on_clauses
                )
                if not matched:
                    expected = " AND ".join(
                        f"{left_key}={right_key}"
                        for left_key, right_key in zip(left_keys, right_keys)
                    )
                    issues.append(
                        f"JOIN 键不匹配：方案要求 {expected}，但 SQL ON 条件中未完整找到"
                    )

    return issues


def validate_sql_node(state: AgentState):
    generated_sql = state.get("generated_sql", "")

    # 缺少 LIMIT → 程序自动追加，无需 LLM 重生成（所有 Hive 查询都必须带 LIMIT）
    sql_clean = generated_sql.rstrip(";").strip()
    if "LIMIT" not in sql_clean.upper():
        generated_sql = sql_clean + " LIMIT 50"
        log_node_event("validate_sql", "自动追加 LIMIT 50")

    # 打印节点开始日志
    log_node_start("validate_sql", sql=str(generated_sql))

    # 基础合法性校验
    is_valid, message = validate_hive_sql(generated_sql)
    if not is_valid:
        return {
            "sql_valid": False,
            "sql_error": message,

            # Router 将决定重新生成还是结束
            "topic_status": "validating_sql",
        }
    # 资源保护校验
    is_valid, message = validate_sql_with_guardrails(generated_sql)
    if not is_valid:
        return {
            "sql_valid": False,
            "sql_error": message,

            # Router 将决定重新生成还是结束
            "topic_status": "validating_sql",
        }

    # 复杂查询 AST 安全校验（Layer 1）
    confirmed_plan = state.get("confirmed_plan") or {}
    complex_flag = confirmed_plan.get("complex", False)
    if complex_flag:
        safety_result = validate_sql_safety_simple(generated_sql)
        if not safety_result.passed:
            log_node_event("validate_sql", f"Complex query safety check failed (Layer {safety_result.layer}): {safety_result.error[:100]}")
            return {
                "sql_valid": False,
                "sql_error": f"[复杂查询安全校验 Layer {safety_result.layer}] {safety_result.error}",
                "topic_status": "validating_sql",
            }

    # 多表安全校验（已有）
    confirmed_plan = state.get("confirmed_plan") or {}
    join_issues = _validate_multi_table(generated_sql, confirmed_plan)
    if join_issues:
        return {
            "sql_valid": False,
            "sql_error": "; ".join(join_issues),

            # Router 将决定重新生成还是结束
            "topic_status": "validating_sql",
        }

    # 逐表过滤属于执行前硬门禁，防止历史分区重复参与Join导致数据膨胀。
    table_filter_issues = validate_table_plan_filters(
        generated_sql,
        confirmed_plan.get("tables") or [confirmed_plan.get("table", "")],
        confirmed_plan.get("table_plans") or [],
    )
    if table_filter_issues:
        return {
            "sql_valid": False,
            "sql_error": "; ".join(table_filter_issues),
            "topic_status": "validating_sql",
        }

    # 打印节点结束日志
    log_node_end("validate_sql", valid=is_valid, error=message)

    return {
        "sql_valid": True,
        "sql_error": "",
        "generated_sql": generated_sql,
        # SQL 已通过校验，下一阶段执行查询
        "topic_status": "executing",
    }
