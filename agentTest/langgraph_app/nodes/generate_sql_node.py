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
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.llm import set_llm_caller
from agentTest.langgraph_app.message_utils import get_last_ai_content
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.services.sql_table_filter_validator import validate_table_plan_filters
from agentTest.langgraph_app.prompts.sql_prompts import (
    SQL_AUDIT_HUMAN_TEMPLATE,
    SQL_AUDIT_SYSTEM_PROMPT,
    SQL_COMPLEX_HUMAN_TEMPLATE,
    SQL_COMPLEX_SYSTEM_PROMPT,
    SQL_CONSISTENCY_FIX_HUMAN_TEMPLATE,
    SQL_CONSISTENCY_FIX_SYSTEM_PROMPT,
    SQL_FIX_HUMAN_TEMPLATE,
    SQL_FIX_SYSTEM_PROMPT,
)

MAX_CONSISTENCY_RETRIES = 2  # 方案一致性校验最多重试次数

def _format_examples(docs: list) -> str:
    """将检索到的历史优质示例转为 Few-shot 文本，LLM 自行从完整 SQL 中学模式"""
    if not docs:
        return ""
    lines = ["【历史相似查询示例 —— 请重点关注 SQL 中的聚合方式、GROUP BY 维度、日期处理】"]
    for i, doc in enumerate(docs, 1):
        q = doc.metadata.get("effective_query") or doc.metadata.get("question", "")
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


def _normalize_join_keys(keys) -> list[str]:
    """统一单字段和复合字段Join键格式。"""
    if isinstance(keys, str):
        return [keys]
    return [key for key in (keys or []) if isinstance(key, str) and key]


def _invert_join_type(join_type: str) -> str:
    """Join边反向使用时同步反转左右连接类型。"""
    normalized = (join_type or "LEFT").upper()
    if normalized == "LEFT":
        return "RIGHT"
    if normalized == "RIGHT":
        return "LEFT"
    return normalized


def _build_fallback_sql(confirmed_plan: dict) -> str:
    """根据 confirmed_plan 构造标准 SQL，多表时使用 field_sources 为维度字段加表别名"""
    tables = confirmed_plan.get("tables") or []
    table = confirmed_plan.get("table", "") or (tables[0] if tables else "")
    measures = confirmed_plan.get("measures", [])
    dimensions = confirmed_plan.get("dimensions", [])
    time_field = confirmed_plan.get("time_field", "pt_dt")
    filters = confirmed_plan.get("filters", "")
    joins = confirmed_plan.get("joins") or []  # 多表Join边
    field_sources = confirmed_plan.get("field_sources") or {}  # {字段名: db.table}
    having = confirmed_plan.get("having", "")

    if not table or not measures:
        return ""

    date_expr = DATE_FORMAT_HIVE_EXPR.get("yyyyMMdd", "date_sub(current_date(), 1)")

    # 多表：主表 + JOIN 子句，使用短表名作为别名
    def _short_name(full_name: str) -> str:
        """从 database.table 中提取 table 短名"""
        return full_name.split(".")[-1] if "." in full_name else full_name

    # 多表时根据 field_sources 给字段加表别名，field_sources 为空则回退到主表
    def _qualify(field_name: str) -> str:
        if not joins:
            return field_name
        source_table = field_sources.get(field_name, table)
        return f"{_get_alias(source_table)}.{field_name}"

    # 使用简短别名 a, b, c...，维护表名->别名映射
    _alias_counter = [0]
    _table_alias_map = {}
    def _get_alias(table_name: str) -> str:
        if table_name not in _table_alias_map:
            _alias_counter[0] += 1
            _table_alias_map[table_name] = chr(96 + _alias_counter[0])  # a, b, c...
        return _table_alias_map[table_name]
    left_alias = _get_alias(table)

    select_parts = []
    for dim in dimensions:
        select_parts.append(_qualify(dim))
    for m in measures:
        select_parts.append(f"SUM({_qualify(m)}) AS {m}")

    from_clause = f"FROM {table} {left_alias}"

    # 逐表 WHERE：根据 table_plans 为每张表生成时间过滤条件
    table_plans = confirmed_plan.get("table_plans") or []
    table_conditions: dict[str, list[str]] = {}
    if table_plans:
        # 按 table_plans 逐表生成过滤条件，右表条件稍后放入JOIN ON以保留外连接语义。
        for tp in table_plans:
            tp_table = tp.get("table", "")
            tp_alias = _get_alias(tp_table)
            tp_time = tp.get("time_field", "pt_dt")
            tp_filters = tp.get("filters", "")
            tp_date_expr = DATE_FORMAT_HIVE_EXPR.get("yyyyMMdd", "date_sub(current_date(), 1)")
            table_conditions.setdefault(tp_table, []).append(
                f"{tp_alias}.{tp_time} = {tp_date_expr}"
            )
            if tp_filters and tp_filters.strip():
                table_conditions.setdefault(tp_table, []).append(
                    f"{tp_alias}.{tp_filters.strip()}"
                )
    else:
        # 兼容旧格式：只用全局 time_field
        qualified_time = f"{left_alias}.{time_field}" if joins else time_field
        table_conditions[table] = [f"{qualified_time} = {date_expr}"]
        if filters and filters.strip():
            table_conditions[table].append(filters.strip())

    where_parts = list(table_conditions.pop(table, []))
    joined_tables = {table}
    pending_edges = list(joins)
    while pending_edges:
        edge_added = False
        for edge in list(pending_edges):
            edge_left_table = edge["left_table"]
            edge_right_table = edge["right_table"]
            left_keys = _normalize_join_keys(edge["left_key"])
            right_keys = _normalize_join_keys(edge["right_key"])
            if len(left_keys) != len(right_keys) or not left_keys:
                return ""

            if edge_left_table in joined_tables and edge_right_table not in joined_tables:
                source_table = edge_left_table
                target_table = edge_right_table
                source_keys = left_keys
                target_keys = right_keys
                join_type = edge.get("join_type", "LEFT")
            elif edge_right_table in joined_tables and edge_left_table not in joined_tables:
                source_table = edge_right_table
                target_table = edge_left_table
                source_keys = right_keys
                target_keys = left_keys
                join_type = _invert_join_type(edge.get("join_type", "LEFT"))
            else:
                continue

            source_alias = _get_alias(source_table)
            target_alias = _get_alias(target_table)
            on_parts = [
                f"{source_alias}.{source_key} = {target_alias}.{target_key}"
                for source_key, target_key in zip(source_keys, target_keys)
            ]
            # 被连接表的分区和过滤条件放入ON，避免LEFT JOIN被WHERE条件退化为INNER JOIN。
            on_parts.extend(table_conditions.pop(target_table, []))
            from_clause += (
                f"\n{join_type} JOIN {target_table} {target_alias} "
                f"ON {' AND '.join(on_parts)}"
            )
            joined_tables.add(target_table)
            pending_edges.remove(edge)
            edge_added = True

        if not edge_added:
            return ""
    sql_lines = [
        f"SELECT {', '.join(select_parts)}",
        from_clause,
        f"WHERE {' AND '.join(where_parts) if where_parts else '1 = 1'}",
    ]
    if dimensions:
        group_parts = [_qualify(d) for d in dimensions]
        sql_lines.append(f"GROUP BY {', '.join(group_parts)}")
    # HAVING 聚合后过滤
    if having and having.strip():
        sql_lines.append(f"HAVING {having.strip()}")
    # ORDER BY
    order_by = confirmed_plan.get("order_by") or []
    if order_by:
        order_items = [f"{item['field']} {item.get('direction', 'ASC')}" for item in order_by]
        sql_lines.append(f"ORDER BY {', '.join(order_items)}")
    # LIMIT
    result_limit = confirmed_plan.get("result_limit", 1000)
    sql_lines.append(f"LIMIT {result_limit}")
    return "\n".join(sql_lines)


def _repair_missing_table_filters(sql: str, confirmed_plan: dict) -> tuple[str, list[str]]:
    """简单查询缺少逐表过滤时，使用已确认方案确定性重建安全SQL。"""
    tables = confirmed_plan.get("tables") or []
    table_plans = confirmed_plan.get("table_plans") or []
    filter_issues = validate_table_plan_filters(sql, tables, table_plans)
    if not filter_issues or confirmed_plan.get("complex", False):
        return sql, filter_issues

    # 当前确定性构造器只支持“昨天”，其他时间范围继续交给现有LLM重试链。
    unsupported_ranges = [
        table_plan.get("time_range", "")
        for table_plan in table_plans
        if table_plan.get("time_range") and "昨天" not in table_plan.get("time_range", "")
    ]
    if unsupported_ranges:
        return sql, filter_issues

    fallback_sql = _build_fallback_sql(confirmed_plan)
    if not fallback_sql:
        return sql, filter_issues

    repaired_issues = validate_table_plan_filters(
        fallback_sql,
        tables,
        table_plans,
    )
    if repaired_issues:
        return sql, filter_issues
    return fallback_sql, filter_issues


def _validate_sql_against_plan(sql: str, confirmed_plan: dict) -> list:
    issues = []
    sql_upper = sql.upper()
    tables = confirmed_plan.get("tables") or []
    table = confirmed_plan.get("table", "") or (tables[0] if tables else "")
    measures = confirmed_plan.get("measures", [])
    dimensions = confirmed_plan.get("dimensions", [])
    time_field = confirmed_plan.get("time_field", "pt_dt")
    filters = confirmed_plan.get("filters", "")

    if not table:
        return issues

    # 1. 表名校验（支持多表）
    for t in tables:
        t_normalized = t.replace(".", "\\.").lower()
        if not re.search(t_normalized, sql.lower()):
            issues.append(f"SQL 中未找到方案指定的表: {t}")

    # 2. 度量字段校验
    for m in measures:
        # 多表SQL允许使用表别名前缀，例如SUM(a.new_order)。
        agg_pattern = (
            r"(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*"
            r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
            + re.escape(m)
            + r"\s*\)"
        )
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

    # 5. 多表逐表过滤校验：每张表都必须实际使用自身table_plan中的过滤字段。
    table_filter_issues = validate_table_plan_filters(
        sql,
        tables or [table],
        confirmed_plan.get("table_plans") or [],
    )
    issues.extend(table_filter_issues)

    # 6. filters 校验
    if filters and filters.strip():
        filter_normalized = filters.strip().lower().replace(" ", "")
        sql_normalized = sql.lower().replace(" ", "")
        if filter_normalized not in sql_normalized:
            issues.append(f"过滤条件 '{filters}' 未出现在 SQL 的 WHERE 中")

    return issues


def _check_filter_enum_values(sql: str, enum_lookup_simple: dict) -> list:
    """校验 SQL 过滤条件中的字符串字面量是否命中字段枚举值，防止模型猜测。"""
    if not enum_lookup_simple:
        return []
    import re as _re
    issues = []
    pattern = _re.compile(
        r"(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']+)'"
    )
    for match in pattern.finditer(sql or ""):
        field = match.group(1)
        value = match.group(2)
        candidates = enum_lookup_simple.get(field, [])
        if candidates and value not in candidates:
            issues.append(
                f"过滤条件 {field} = '{value}' 不在枚举值 {candidates} 中，请改为枚举内值"
            )
    return issues


def _check_plan_consistency(sql: str, confirmed_plan: dict, advisor_last_answer: str, llm, enum_lookup_simple: dict = None) -> str:
    tables = confirmed_plan.get("tables", [])
    fields = confirmed_plan.get("fields", [])
    measures = confirmed_plan.get("measures", []) or confirmed_plan.get("fields", [])
    dimensions = confirmed_plan.get("dimensions", [])
    time_field = confirmed_plan.get("time_field", "pt_dt")
    filters = confirmed_plan.get("filters", "")
    joins = confirmed_plan.get("joins") or []  # 多表Join边
    table_plans = confirmed_plan.get("table_plans") or []

    # ── 先做程序化校验 ──
    programmatic_issues = _validate_sql_against_plan(sql, confirmed_plan)
    if programmatic_issues:
        return "; ".join(programmatic_issues)

    # ── 过滤条件枚举值校验：字面量必须命中字段枚举，避免模型猜测 ──
    enum_issues = _check_filter_enum_values(sql, enum_lookup_simple)
    if enum_issues:
        return "; ".join(enum_issues)

    # ── 构造逐表过滤描述 ──
    table_plans_desc = "\n".join(
        f"  {table_plan.get('table', '')}: "
        f"time_field={table_plan.get('time_field', '')}, "
        f"time_range={table_plan.get('time_range', '')}, "
        f"filters={table_plan.get('filters', '') or '无'}"
        for table_plan in table_plans
    )

    # ── 构造Join描述 ──
    joins_desc = ""
    if joins and len(tables) > 1:
        join_lines = []
        for edge in joins:
            join_lines.append(
                f"  {edge['left_table']}.{edge['left_key']} = "
                f"{edge['right_table']}.{edge['right_key']} ({edge.get('join_type', 'LEFT')} JOIN)"
            )
        joins_desc = "\n".join(join_lines)
    elif len(tables) > 1:
        # 多表无关联关系：标记为 independent_aggregation 策略，避免校验器误判为"无 JOIN"
        joins_desc = (
            "independent_aggregation（多表无主外键约束，按各自粒度独立聚合，"
            "用 CROSS JOIN 子查询 或 UNION ALL 合并，禁止 JOIN ON <字段>=<字段>）"
        )

    # ── 程序化校验通过，再用 LLM 做语义校验 ──
    check_prompt = ChatPromptTemplate.from_messages([
        ("system", SQL_AUDIT_SYSTEM_PROMPT),
        ("human", SQL_AUDIT_HUMAN_TEMPLATE),
    ])

    prompt_value = check_prompt.invoke({
        "table": ", ".join(tables),
        "measures": ", ".join(measures) if measures else "无（仅查询维度信息）",
        "dimensions": ", ".join(dimensions) if dimensions else "无",
        "time_field": time_field,
        "table_plans": table_plans_desc or "无",
        "filters": filters or "无",
        "joins": joins_desc or "单表查询（无需表关联）",
        "advisor_answer": advisor_last_answer[:1200],
        "sql": sql,
    })
    result = llm.invoke(prompt_value)
    content_result = result.content.strip() if hasattr(result, 'content') else str(result).strip()

    if content_result.upper().startswith("PASS"):
        return ""
    return content_result


def build_generate_sql_node(runtime):
    llm = runtime["llm"]
    default_prompt = runtime["prompt"]

    def generate_sql_node(state: AgentState) -> dict:
        # 标记调用方，LLM 日志按业务方归类（生成 / 修复 / 一致性校验）
        set_llm_caller("generate_sql")
        confirmed_plan = state.get("confirmed_plan") or {}
        # 企业级规则：走到 generate_sql 的请求必须有 confirmed_plan
        if not confirmed_plan.get("table") and not confirmed_plan.get("tables"):
            log_node_error("generate_sql", error="缺少confirmed_plan，无法生成SQL（应在此之前由Planner兜底路由Advisor）")
            return {
                "generated_sql": "",
                "sql_error": "缺少已确认的分析方案",
                "topic_status": "failed",
            }
        confirmed_section = ""
        if confirmed_plan.get("table") or confirmed_plan.get("tables"):
            tables = confirmed_plan.get("tables") or []
            table = confirmed_plan.get("table", "") or (tables[0] if tables else "")
            measures = confirmed_plan.get("measures", [])
            dimensions = confirmed_plan.get("dimensions", [])
            time_field = confirmed_plan.get("time_field", "pt_dt")
            joins = confirmed_plan.get("joins") or []  # 多表Join边
            field_sources = confirmed_plan.get("field_sources") or {}  # 字段来源映射
            filters = confirmed_plan.get("filters", "")

            # 涉及表列表（多表时用逗号拼接）
            tables_str = ", ".join(tables)
            parts = [f"- 数据表: {tables_str}"]
            if measures:
                parts.append(f"- 度量字段（必须用聚合函数 SUM/COUNT/AVG）: {', '.join(measures)}")
            if dimensions:
                parts.append(f"- 维度字段（必须在 SELECT 和 GROUP BY 中）: {', '.join(dimensions)}")
            time_range_cs = confirmed_plan.get("time_range", "") or "昨天"
            parts.append(f"- 主表时间分区（必须在 WHERE 中）: {time_field}（{time_range_cs}）")
            if filters:
                parts.append(f"- 额外过滤条件（必须在 WHERE 中）: {filters}")
            # 多表时追加字段来源和JOIN约束以及别名规则
            if len(tables) > 1 and field_sources:
                field_items = [
                    f"  {f} → {field_sources.get(f, '?')}"
                    for f in confirmed_plan.get("fields", [])
                ]
                parts.append("- 字段来源:\n" + "\n".join(field_items))
                # 追加别名规则，LLM 根据 field_sources 自行推导
                alias_lines = []
                seen_aliases = {}
                for f, src in field_sources.items():
                    short = src.split(".")[-1] if "." in src else src
                    seen_aliases[src] = short
                for src_full, src_short in seen_aliases.items():
                    alias_lines.append(f"  {src_full} → 别名: {src_short}")
                parts.append("- 多表别名规则（SELECT 和 GROUP BY 中的每个字段必须加表别名前缀）:\n" + "\n".join(alias_lines))
            if joins:
                join_items = [
                    f"  {e['left_table']} JOIN {e['right_table']} "
                    f"ON {e['left_key']}={e['right_key']} ({e.get('join_type', 'LEFT')})"
                    for e in joins
                ]
                parts.append("- 表关联（严格按此JOIN，不得修改）:\n" + "\n".join(join_items))
            elif len(tables) > 1:
                # 多表无 join 关系：注入 independent_aggregation 策略提示，避免 LLM 误用 JOIN
                parts.append(
                    "- 表关联策略: independent_aggregation（多表无主外键约束，按各自粒度独立聚合后用 CROSS JOIN 子查询 或 UNION ALL 合并；禁止 JOIN ON <字段>=<字段>）"
                )

            # AI 推断 Join 模式：部分表缺少预配置关联关系，LLM 需自行推断 JOIN 条件
            ai_inferred_join = confirmed_plan.get("ai_inferred_join", False)
            if ai_inferred_join:
                parts.append(
                    "- 注意：部分表之间缺少预配置关联关系，请根据各表字段语义推断合适的 JOIN 键（如 company_id、order_id 等）"
                )
            # 每表的时间过滤规则
            table_plans = confirmed_plan.get("table_plans") or []
            if table_plans:
                tp_lines = []
                for tp in table_plans:
                    tp_lines.append(f"  {tp.get('table','')}: time={tp.get('time_field','pt_dt')}({tp.get('time_range','')}), filters={tp.get('filters','') or '无'}")
                parts.append("- 逐表时间过滤（主表放WHERE，被连接表放对应JOIN ON；每张表都必须过滤）:\n" + "\n".join(tp_lines))
            # 排序规则
            order_by = confirmed_plan.get("order_by") or []
            if order_by:
                order_strs = [f"{item['field']} {item.get('direction', 'ASC')}" for item in order_by]
                parts.append(f"- 排序规则（必须 ORDER BY）: {', '.join(order_strs)}")
            # 聚合后过滤
            having = confirmed_plan.get("having", "")
            if having and having.strip():
                parts.append(f"- 聚合后过滤（必须 HAVING）: {having}")
            # 返回行数
            result_limit = confirmed_plan.get("result_limit", 1000)
            if result_limit != 1000:
                parts.append(f"- 返回行数（必须 LIMIT）: {result_limit}")
            # 复杂查询模式
            complex_flag = confirmed_plan.get("complex", False)
            if complex_flag:
                parts.append("- 复杂查询模式：允许使用窗口函数(ROW_NUMBER/RANK)、子查询、CTE，不受平铺 GROUP BY 约束")

            confirmed_section = "【已确认的分析方案 —— 以下规则必须严格遵守】\n" + "\n".join(parts)


        # SQL 生成使用 Planner 改写后的完整有效需求，避免使用“好的”等确认文本
        question = state.get("effective_query") or ""
        schema_context = state["schema_context"]
        # 从统一消息中获取最近一次Advisor确认描述
        advisor_last_answer = get_last_ai_content(
            state.get("messages") or [],
            "advisor",
        )

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

            # 复杂查询模式：使用专门的 prompt，允许窗口函数/子查询/CTE
            complex_flag = confirmed_plan.get("complex", False)
            if complex_flag:
                prompt = ChatPromptTemplate.from_messages([
                    ("system", SQL_COMPLEX_SYSTEM_PROMPT),
                    ("human", SQL_COMPLEX_HUMAN_TEMPLATE),
                ])

            if retry_count > 0 or sql_fix_reason:
                if confirmed_section:
                    prompt_input["schema_context"] = confirmed_section + "\n\n" + schema_context

                previous_sql = state.get("generated_sql", "")
                prompt = ChatPromptTemplate.from_messages([
                    ("system", SQL_FIX_SYSTEM_PROMPT),
                    ("human", SQL_FIX_HUMAN_TEMPLATE),
                ])
                prompt_input["previous_sql"] = previous_sql
                prompt_input["sql_fix_reason"] = sql_fix_reason

            prompt_value = prompt.invoke(prompt_input)
            generated_sql = llm.invoke(prompt_value)
            generated_sql = clear_sql(generated_sql)

            # 简单查询缺少某张表过滤时优先确定性修复，避免带膨胀风险的SQL进入执行链。
            repaired_sql, missing_filter_issues = _repair_missing_table_filters(
                generated_sql,
                confirmed_plan,
            )
            if repaired_sql != generated_sql:
                log_node_event(
                    "generate_sql",
                    "逐表过滤自动修复: " + "; ".join(missing_filter_issues),
                )
                generated_sql = repaired_sql

            # ── 方案一致性校验 ──
            consistency_retry = 0
            enum_lookup_simple = runtime.get("sample_values_map_simple") or {}
            pass_history = list(state.get("sql_pass_history") or [])
            final_sql_for_history = ""
            if confirmed_plan.get("table") or confirmed_plan.get("tables"):
                while consistency_retry < MAX_CONSISTENCY_RETRIES:
                    inconsistency = _check_plan_consistency(
                        generated_sql, confirmed_plan, advisor_last_answer, llm,
                        enum_lookup_simple=enum_lookup_simple,
                    )
                    if not inconsistency:
                        # 记录本次 PASS 的 SQL，供后续 exec_retry 时回灌给 LLM 参考
                        final_sql_for_history = generated_sql
                        break

                    consistency_retry += 1
                    retry_count += 1
                    log_node_start("generate_sql", retry=retry_count,
                                   consistency_fix=inconsistency[:80])

                    fix_prompt = ChatPromptTemplate.from_messages([
                        ("system", SQL_CONSISTENCY_FIX_SYSTEM_PROMPT),
                        ("human", SQL_CONSISTENCY_FIX_HUMAN_TEMPLATE),
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
                            # 降级构造的 SQL 也视作通过校验，加入 history
                            final_sql_for_history = fallback_sql

            # 把本次 PASS 的 SQL 写入 history（去重，最多保留最近 3 条）
            if final_sql_for_history:
                if final_sql_for_history not in pass_history:
                    pass_history.append(final_sql_for_history)
                pass_history = pass_history[-3:]

            log_node_end(
                "generate_sql",
                sql=str(generated_sql),
                ctx_len=len(schema_context),
                ms=elapsed_ms(timer),
            )
            # 节点完成后记录 State 分层摘要，供 trace 查看数据流转
            log_state_snapshot("generate_sql", {**state, **{
                "generated_sql": generated_sql,
                "topic_status": "validating_sql",
            }})

            return {
                "generated_sql": generated_sql,
                "sql_pass_history": pass_history,

                # SQL 已生成，下一阶段进行安全和语法校验
                "topic_status": "validating_sql",
            }
        except Exception as error:
            log_node_error("generate_sql", retry=retry_count, error=str(error), ms=elapsed_ms(timer))
            raise

    return generate_sql_node

