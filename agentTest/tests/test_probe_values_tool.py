# 值探查工具测试：受控只读 LIKE 探查，覆盖安全校验/转义/limit 归一化/格式化输出
import unittest

from agentTest.langgraph_app.tools.probe_values_tool import build_probe_values_tool
from agentTest.validate.sql_validate import is_read_only_sql
from agentTest.validate.sql_validate import validate_hive_sql


class _FakeDatasource:
    """极简数据源桩：记录执行的 SQL，返回预设行。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def query(self, sql, timeout_seconds=None, max_rows=None):
        self.calls.append({"sql": sql, "timeout_seconds": timeout_seconds, "max_rows": max_rows})
        return {"sql": sql, "columns": ["v"], "rows": self.rows, "row_count": len(self.rows)}


class _FakeMetadataProvider:
    """极简元数据桩：返回预设表结构；schema 为空表示表不可用。"""

    def __init__(self, schema=None):
        self.schema = schema

    def describe_table(self, table):
        if not self.schema:
            raise ValueError(f"table not allowed or not exists: {table}")
        return self.schema


_SCHEMA = {
    "database_name": "ads_trip",
    "table_name": "ads_gundam_device_return_detail_hour",
    "table_comment": "",
    "columns": [
        {"name": "region_name", "type": "varchar", "comment": ""},
        {"name": "status", "type": "varchar", "comment": ""},
    ],
}


def _build_tool(datasource=None, provider=None, **kwargs):
    datasource = datasource or _FakeDatasource([("徐州大区",), ("徐州二区",)])
    provider = provider or _FakeMetadataProvider(_SCHEMA)
    return build_probe_values_tool(datasource, provider, **kwargs)


class ProbeValuesToolTest(unittest.TestCase):
    """probe_values 工具：格式化输出、SQL 安全、LIKE 转义、limit 归一化。"""

    def test_returns_formatted_values(self):
        """正常探查：返回字段实际存储值，SQL 带 LIKE/LIMIT 且表白名单裸写。"""
        ds = _FakeDatasource([("徐州大区",), ("徐州二区",)])
        tool = _build_tool(ds)
        out = tool.invoke({"table": "ads_trip.ads_gundam_device_return_detail_hour", "column": "region_name", "keyword": "徐州"})
        self.assertIn("徐州大区", out)
        self.assertIn("徐州二区", out)
        self.assertIn("字段 ads_trip.ads_gundam_device_return_detail_hour.region_name 的实际存储值", out)
        sql = ds.calls[0]["sql"]
        self.assertIn("LIKE '%徐州%'", sql)
        self.assertIn("LIMIT 20", sql)
        self.assertIn("FROM ads_trip.ads_gundam_device_return_detail_hour", sql)
        self.assertEqual(ds.calls[0]["max_rows"], 20)

    def test_escapes_like_special(self):
        """LIKE 特殊字符转义：% _ 不被当作通配符。"""
        ds = _FakeDatasource([("50%_A",)])
        tool = _build_tool(ds)
        tool.invoke({"table": "ads_trip.ads_gundam_device_return_detail_hour", "column": "region_name", "keyword": "50%_A"})
        sql = ds.calls[0]["sql"]
        self.assertIn("LIKE '%50\\%\\_A%'", sql)

    def test_empty_keyword_returns_any_values(self):
        """关键词为空：不加 WHERE，返回该字段任意取值。"""
        ds = _FakeDatasource([("cos",)])
        tool = _build_tool(ds)
        tool.invoke({"table": "ads_trip.ads_gundam_device_return_detail_hour", "column": "region_name", "keyword": ""})
        sql = ds.calls[0]["sql"]
        self.assertNotIn("WHERE", sql)
        self.assertIn("LIMIT 20", sql)

    def test_rejects_unknown_column(self):
        """字段不存在：不执行 SQL，直接返回提示。"""
        ds = _FakeDatasource()
        tool = _build_tool(ds)
        out = tool.invoke({"table": "ads_trip.ads_gundam_device_return_detail_hour", "column": "not_exist", "keyword": "x"})
        self.assertIn("不存在", out)
        self.assertEqual(ds.calls, [])

    def test_rejects_unallowed_table(self):
        """表非白名单/不存在：describe_table 抛错时直接返回提示，不执行 SQL。"""
        ds = _FakeDatasource()
        tool = _build_tool(ds, provider=_FakeMetadataProvider(None))
        out = tool.invoke({"table": "evil_db.evil_table", "column": "x", "keyword": "x"})
        self.assertIn("表不可用", out)
        self.assertEqual(ds.calls, [])

    def test_clamps_limit_over_max(self):
        """limit 超上限截断到上限。"""
        ds = _FakeDatasource()
        tool = _build_tool(ds)
        tool.invoke({"table": "ads_trip.ads_gundam_device_return_detail_hour", "column": "region_name", "keyword": "", "limit": 999})
        self.assertEqual(ds.calls[0]["max_rows"], 50)
        self.assertIn("LIMIT 50", ds.calls[0]["sql"])

    def test_sql_passes_readonly_and_hive_validation(self):
        """生成的探查 SQL 必须通过只读与 Hive 白名单/LIMIT 校验（安全层闭环）。"""
        ds = _FakeDatasource()
        tool = _build_tool(ds)
        tool.invoke({"table": "ads_trip.ads_gundam_device_return_detail_hour", "column": "status", "keyword": "同意"})
        sql = ds.calls[0]["sql"]
        self.assertTrue(is_read_only_sql(sql)[0])
        self.assertTrue(validate_hive_sql(sql)[0])


if __name__ == "__main__":
    unittest.main()
