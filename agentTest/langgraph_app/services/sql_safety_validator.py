# SQL 安全校验器：三层防线
# Layer 1: AST 解析 - 校验表白名单、禁止危险操作、检测笛卡尔积
# Layer 2: Hive EXPLAIN - 发送 EXPLAIN 到 Hive，验证执行计划
# Layer 3: LIMIT 0 试跑 - 实际编译验证，Hive 编译器级别全量检查
# 三层全过才执行真实查询；任一失败带错误信息重试 LLM

import re
import sqlparse
from sqlparse.sql import IdentifierList, Identifier, Where, Comparison
from sqlparse.tokens import Keyword, DML, Name, Punctuation
from agentTest.db.hive_guardrails import is_table_allowed
from agentTest.langgraph_app.runtime.graph_logger import log_node_event, log_node_error


class SafetyCheckResult:
    """安全校验结果"""
    def __init__(self, passed: bool, error: str = "", layer: int = 0):
        self.passed = passed
        self.error = error      # 错误描述，供 LLM 修正
        self.layer = layer      # 失败发生在第几层 (1/2/3)


def validate_sql_safety(sql: str, hive_datasource=None) -> SafetyCheckResult:
    """三层防线，返回第一个失败的防线"""

    # ---- Layer 1: AST 解析 ----
    try:
        parsed = sqlparse.parse(sql)
        if not parsed:
            return SafetyCheckResult(False, "SQL 解析失败：无法解析 SQL 语句", layer=1)

        statement = parsed[0]

        # 1a. 检查危险操作
        sql_upper = sql.upper()
        dangerous = ["DROP ", "DELETE ", "TRUNCATE ", "ALTER ", "INSERT ", "UPDATE ", "CREATE "]
        for d in dangerous:
            if d in sql_upper:
                return SafetyCheckResult(False, f"禁止的操作: {d.strip()}", layer=1)

        # 1b. 检查表在白名单（配置支持 db.table 精确匹配）
        tables_in_sql = _extract_tables(statement)
        for t in tables_in_sql:
            db_name = t.split(".")[0] if "." in t else ""
            table_short = t.split(".")[-1] if "." in t else t
            if not is_table_allowed(table_short, db_name):
                return SafetyCheckResult(
                    False,
                    f"表 {t} 不在白名单中",
                    layer=1
                )

        # 1c. 检测笛卡尔积（逗号分隔多表）
        if _detect_cartesian(sql):
            return SafetyCheckResult(
                False,
                "检测到逗号分隔多表（笛卡尔积风险），请使用 JOIN ... ON 语法",
                layer=1
            )

    except Exception as e:
        return SafetyCheckResult(False, f"AST 解析异常: {str(e)}", layer=1)

    # ---- Layer 2: Hive EXPLAIN (if datasource available) ----
    if hive_datasource:
        try:
            explain_sql = f"EXPLAIN {sql}"
            result = hive_datasource.query(explain_sql)
            if result is None:
                return SafetyCheckResult(False, "Hive EXPLAIN 返回空结果", layer=2)
        except Exception as e:
            error_str = str(e)
            return SafetyCheckResult(
                False,
                f"Hive EXPLAIN 失败: {error_str[:300]}",
                layer=2
            )

    # ---- Layer 3: LIMIT 0 试跑 ----
    if hive_datasource:
        try:
            trial_sql = sql.rstrip(";").strip()
            # Replace LIMIT with LIMIT 0
            trial_sql = re.sub(r"LIMIT\s+\d+", "LIMIT 0", trial_sql, flags=re.IGNORECASE)
            if "LIMIT" not in trial_sql.upper():
                trial_sql += " LIMIT 0"
            hive_datasource.query(trial_sql)
        except Exception as e:
            error_str = str(e)
            return SafetyCheckResult(
                False,
                f"Hive 编译失败: {error_str[:500]}",
                layer=3
            )

    return SafetyCheckResult(True)


def _extract_tables(statement) -> list[str]:
    """从 SQL AST 中提取所有表名"""
    tables = []
    from_seen = False

    def _walk(token):
        nonlocal from_seen
        if token.is_group:
            # ???? db.table ?????? from dim_trip.dim_company_snapshot_day a?
            if from_seen:
                parts = []
                for t in token.tokens:
                    if t.ttype is Name:
                        parts.append(t.value.strip().strip("`\"'"))
                    elif t.ttype is Punctuation and t.value == "." and parts:
                        continue
                    else:
                        break
                if len(parts) >= 2:
                    tables.append(".".join(parts[:2]))
                    from_seen = False
                    return
            for t in token.tokens:
                _walk(t)
            return

        if token.ttype is Keyword and token.value.upper() == "FROM":
            from_seen = True
            return
        if token.ttype is Keyword and token.value.upper() in ("JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS"):
            from_seen = True
            return

        if from_seen and token.ttype is Name:
            name = token.value.strip().strip("`\"'")
            if name and name.upper() not in ("SELECT", "WHERE", "ON", "AND", "OR", "AS", "GROUP", "ORDER", "BY", "HAVING", "LIMIT", "LEFT", "RIGHT", "INNER", "OUTER"):
                tables.append(name)
                from_seen = False
        elif from_seen and isinstance(token, Identifier):
            name = token.get_real_name()
            if name:
                tables.append(name)
            from_seen = False

    for token in statement.tokens:
        _walk(token)

    return list(set(tables))


def _detect_cartesian(sql: str) -> bool:
    """检测逗号分隔的多表（笛卡尔积）"""
    sql_upper = sql.upper()
    # Find FROM clause
    from_match = re.search(
        r"FROM\s+(.+?)(?:WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|$|\))",
        sql_upper, re.DOTALL
    )
    if from_match:
        from_clause = from_match.group(1)
        # If comma in FROM clause and no JOIN keyword, it might be cartesian
        if "," in from_clause and "JOIN" not in from_clause:
            return True
    return False


def validate_sql_safety_simple(sql: str) -> SafetyCheckResult:
    """仅做 AST 层校验（无 Hive 连接时使用）"""
    return validate_sql_safety(sql, hive_datasource=None)
