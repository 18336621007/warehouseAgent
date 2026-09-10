# 值探查工具：Planner 收到 0 行反馈时，用 LIKE 实时探查字段实际存储值，修正过滤条件。
# 参考 Codex 做法：查不到数据时返回用 LIKE 确认具体值，而不是依赖元数据采样猜测。
import re

from agentTest.db.hive_guardrails import QUERY_TIMEOUT_SECONDS
from agentTest.validate.sql_validate import is_read_only_sql
from agentTest.validate.sql_validate import validate_hive_sql
from agentTest.config.planner import PROBE_VALUES_LIMIT_DEFAULT
from agentTest.config.planner import PROBE_VALUES_LIMIT_MAX

# LIKE 特殊字符（% _ \），转义避免关键词被当作通配符匹配越界
_LIKE_SPECIAL = re.compile(r"[%_\\]")


def _escape_like(keyword: str) -> str:
    """转义 LIKE 特殊字符，防止关键词被当作通配符。"""
    return _LIKE_SPECIAL.sub(lambda m: "\\" + m.group(0), str(keyword))


def _clamp_limit(limit, default, max_value):
    """limit 归一化：非法值回退默认，超上限截断到上限。"""
    try:
        limit_int = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        limit_int = default
    return max(1, min(limit_int, max_value))


def build_probe_values_tool(datasource, metadata_provider, limit_default=PROBE_VALUES_LIMIT_DEFAULT, limit_max=PROBE_VALUES_LIMIT_MAX):
    """构建受控只读"值探查"工具，安全边界全部在程序层，不依赖 LLM。

    - 只生成 SELECT DISTINCT ... LIKE ...，天然只读；
    - 表/库先经 metadata_provider.describe_table 做白名单与存在性校验；
    - column 必须是该表真实存在的字段；
    - LIKE 关键词转义、limit 有上限、带超时，防止注入与资源滥用。
    """
    from langchain.tools import tool

    @tool
    def probe_values(table: str, column: str, keyword: str = "", limit: int = None) -> str:
        """实时探查某表某字段的实际存储值（LIKE 模糊匹配），用于确认过滤值是否与库中一致。

        当查询结果为空、怀疑过滤值与实际存储值不一致（如名称被截断、格式不同）时调用；
        返回库中真实取值，供修正 filters 使用。
        参数：
        - table: 表全名，如 ads_trip.ads_gundam_device_return_detail_hour
        - column: 要探查的字段名，如 region_name
        - keyword: 模糊匹配关键词，如 "徐州"；为空时返回该字段任意取值
        - limit: 最多返回取值个数，默认 20，最大 50
        """
        table = str(table or "").strip()
        column = str(column or "").strip()
        if not table or not column:
            return "参数错误：table 与 column 不能为空。"

        # 表白名单与存在性校验（describe_table 内部完成，非白名单直接抛错）
        try:
            schema = metadata_provider.describe_table(table)
        except Exception as error:
            return f"表不可用：{error}"

        # 字段必须是该表真实存在的列，避免探查任意列
        columns = schema.get("columns") or []
        real_column = None
        for c in columns:
            if str(c.get("name") or "").lower() == column.lower():
                real_column = c["name"]
                break
        if real_column is None:
            # 字段不存在：提示改用 search_columns 检索正确字段名，让 LLM 自动切换到字段核验路径
            return f"字段 {column} 不存在于表 {table} 中，无法探查。可改用 search_columns 检索该表正确字段名后重试。"
        limit_int = _clamp_limit(limit, limit_default, limit_max)

        database_name = schema.get("database_name") or ""
        table_name = schema.get("table_name") or ""
        # 表名/库名来自元数据（简单标识符）裸写，让 validate_hive_sql 的表白名单检查生效；
        # 字段名加反引号防保留字干扰。
        quoted_col = f"`{real_column}`"
        quoted_from = f"{database_name}.{table_name}"

        # 关键词为空：返回该字段任意取值；非空：LIKE 转义后模糊匹配
        keyword = str(keyword or "").strip()
        if keyword:
            escaped = _escape_like(keyword)
            where_clause = f" WHERE {quoted_col} LIKE '%{escaped}%'"
        else:
            where_clause = ""
        sql = f"SELECT DISTINCT {quoted_col} AS v FROM {quoted_from}{where_clause} LIMIT {limit_int}"

        # 程序层安全校验：只读 + Hive 白名单/LIMIT 校验（与业务查询同一套安全底线）
        is_valid, message = is_read_only_sql(sql)
        if not is_valid:
            return f"探查 SQL 未通过只读校验：{message}"
        is_valid, message = validate_hive_sql(sql)
        if not is_valid:
            return f"探查 SQL 未通过 Hive 校验：{message}"

        try:
            result = datasource.query(sql, timeout_seconds=QUERY_TIMEOUT_SECONDS, max_rows=limit_int)
        except Exception as error:
            return f"值探查执行失败：{error}"

        rows = (result or {}).get("rows") or []
        values = [str(r[0]) for r in rows if r and r[0] is not None]
        if not values:
            return f"未在 {database_name}.{table_name}.{column} 探查到匹配'{keyword or '任意值'}'的值（0 行）。"

        lines = [f"字段 {database_name}.{table_name}.{column} 的实际存储值（前 {len(values)} 个）："]
        lines += [f"- {v}" for v in values]
        lines.append("如需修正过滤条件，请使用以上精确值（注意名称是否被截断、大小写、空格等）。")
        return "\n".join(lines)

    return probe_values
