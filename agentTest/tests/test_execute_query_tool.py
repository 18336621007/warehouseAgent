# execute_query 工具单元测试：纯执行 SQL + 安全 + 落盘 + 结果摘要
# 覆盖：单条 SQL 执行 / LIMIT 追加 / 引擎路由（data_project→doris，其余→trino/hive 兜底）/
#       跨引擎拒绝 / 校验失败不降级 / 0 行提示 / steps 多段并行 / 已执行 SQL 记录
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from agentTest.langgraph_app.tools.execute_query_tool import (
    build_execute_query_tool,
    set_execute_query_context,
    reset_execute_query_context,
    take_executed_sqls,
)


class _FakeTool:
    """模拟 sql_query_{engine} 工具：记录调用参数，返回预设结果或抛错。"""

    def __init__(self, name, result=None, error=None, call_log=None):
        self.name = name
        self._result = result if result is not None else {"columns": [], "rows": [], "row_count": 0}
        self._error = error
        self.calls = [] if call_log is None else call_log

    def invoke(self, args):
        self.calls.append(dict(args))
        if self._error:
            raise self._error
        return self._result


class _FakeRegistry:
    """最小工具注册表：get_by_name 返回带 .tool 属性的对象（对齐真实 ToolSpec 结构）。"""

    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def get_by_name(self, name):
        if name not in self._tools:
            raise KeyError(name)
        return SimpleNamespace(tool=self._tools[name])


class _FakeSemProvider:
    """最小语义层 provider：按表返回分区字段（用于 partition_fields 透传测试）。"""

    def __init__(self, part_map=None):
        self._part = part_map or {}

    def get_partition_fields(self, table):
        return self._part.get(table, [])


def _default_result():
    return {
        "columns": ["company_name", "new_rent_counts"],
        "row_count": 2,
        "rows": [
            ["科斯特", 120],
            ["锂纳斯", 80],
        ],
    }


def _runtime(result=None, fail_engine=None, part_map=None):
    """构建最小 runtime：引擎工具注册表 + 语义层 provider + 可选失败引擎。"""
    def _mk(engine):
        if fail_engine == engine:
            return _FakeTool(f"sql_query_{engine}", error=RuntimeError(f"{engine} boom"))
        return _FakeTool(f"sql_query_{engine}", result=result if result is not None else _default_result())

    reg = _FakeRegistry([_mk(e) for e in ("trino", "hive", "doris")])
    return {"tool_registry": reg, "semantic_metadata_provider": _FakeSemProvider(part_map)}


class ExecuteQueryToolTest(unittest.TestCase):
    """查数工具：纯执行 SQL + 安全校验 + 落盘 + 结果摘要回填 Agent。"""

    def setUp(self):
        self._tokens = set_execute_query_context("req-1", "conv1", "topic-1")
        # 清空上一个测试残留的已执行 SQL 记录，避免跨测试累积干扰
        take_executed_sqls("req-1")

    def tearDown(self):
        reset_execute_query_context(self._tokens)

    def _call(self, runtime, **kwargs):
        """构建工具并调用 execute_query，返回输出文本。"""
        with mock.patch(
            "agentTest.langgraph_app.services.result_store.save_query_result",
            return_value={"result_id": "req-1:result", "round_no": 1, "full_csv": "D:/tmp/conv1/r1.csv"},
        ):
            tool = build_execute_query_tool(runtime)
            out = tool.invoke(kwargs)
        return out

    def test_execute_simple_sql(self):
        """单条 SQL：执行成功并返回结果摘要（行数 + 预览 + SQL）。"""
        runtime = _runtime()
        out = self._call(runtime, sql="SELECT company_name, SUM(new_rent_counts) AS c FROM ads_trip.tbl WHERE pt_dt = '20260919'")
        self.assertIn("查询成功", out)
        self.assertIn("科斯特", out)
        self.assertIn("实际执行 SQL", out)
        # trino 应被调用且收到默认 partition_fields
        trino = runtime["tool_registry"].get_by_name("sql_query_trino").tool
        self.assertEqual(len(trino.calls), 1)
        self.assertEqual(trino.calls[0]["partition_fields"], ["pt_dt"])

    def test_missing_sql_returns_error(self):
        """sql 为空：返回参数错误，不调用引擎。"""
        runtime = _runtime()
        out = self._call(runtime, question="查数据")
        self.assertIn("参数错误", out)

    def test_limit_appended_when_missing(self):
        """SQL 缺 LIMIT：程序安全追加默认 LIMIT 50。"""
        runtime = _runtime()
        self._call(runtime, sql="SELECT company_name FROM ads_trip.tbl WHERE pt_dt = '20260919'")
        trino = runtime["tool_registry"].get_by_name("sql_query_trino").tool
        self.assertIn("LIMIT 50", trino.calls[0]["sql"])

    def test_result_limit_used_in_limit(self):
        """传 result_limit：SQL 缺 LIMIT 时追加指定行数。"""
        runtime = _runtime()
        self._call(runtime, sql="SELECT company_name FROM ads_trip.tbl WHERE pt_dt = '20260919'", result_limit=10)
        trino = runtime["tool_registry"].get_by_name("sql_query_trino").tool
        self.assertIn("LIMIT 10", trino.calls[0]["sql"])

    def test_trino_failure_fallback_to_hive(self):
        """trino 失败：降级到 hive 兜底执行成功。"""
        runtime = _runtime(fail_engine="trino")
        out = self._call(runtime, sql="SELECT company_name FROM ads_trip.tbl WHERE pt_dt = '20260919'")
        self.assertIn("查询成功", out)
        hive = runtime["tool_registry"].get_by_name("sql_query_hive").tool
        self.assertEqual(len(hive.calls), 1)

    def test_validation_error_no_fallback(self):
        """校验失败（ValueError）：引擎无关，不降级，返回具体原因。"""
        reg = _FakeRegistry([
            _FakeTool("sql_query_trino", error=ValueError("SQL 使用了非白名单表")),
            _FakeTool("sql_query_hive", result=_default_result()),
        ])
        runtime = {"tool_registry": reg, "semantic_metadata_provider": _FakeSemProvider()}
        out = self._call(runtime, sql="SELECT 1 FROM bad_db.tbl WHERE pt_dt = '20260919'")
        self.assertIn("安全校验", out)
        self.assertIn("非白名单表", out)
        hive = runtime["tool_registry"].get_by_name("sql_query_hive").tool
        self.assertEqual(len(hive.calls), 0)

    def test_cross_engine_rejected(self):
        """跨引擎多表（data_project + 其余库）：拒绝并提示拆开。"""
        runtime = _runtime()
        sql = "SELECT a.x, b.y FROM data_project.d_tbl a JOIN ads_trip.a_tbl b ON a.id = b.id WHERE a.pt_dt = '20260919'"
        out = self._call(runtime, sql=sql)
        self.assertIn("跨引擎", out)

    def test_zero_row_note(self):
        """0 行结果：摘要提示过滤值与实际存储值可能不一致。"""
        runtime = _runtime(result={"columns": ["company_name"], "row_count": 0, "rows": []})
        out = self._call(runtime, sql="SELECT company_name FROM ads_trip.tbl WHERE pt_dt = '20260919'")
        self.assertIn("0 行", out)

    def test_steps_parallel(self):
        """steps 多段：每段独立执行并各自落盘，返回多段摘要。"""
        runtime = _runtime()
        steps = json.dumps([
            {"id": "s1", "sql": "SELECT COUNT(*) AS c FROM ads_trip.t1 WHERE pt_dt = '20260919'", "question": "指标1"},
            {"id": "s2", "sql": "SELECT COUNT(*) AS c FROM ads_trip.t2 WHERE pt_dt = '20260919'", "question": "指标2"},
        ])
        out = self._call(runtime, steps=steps)
        self.assertIn("已并行执行多段查询", out)
        self.assertIn("[s1]", out)
        self.assertIn("[s2]", out)

    def test_steps_parse_error(self):
        """steps 非法：返回解析失败提示。"""
        runtime = _runtime()
        out = self._call(runtime, steps="not-json")
        self.assertIn("steps 解析失败", out)

    def test_executed_sql_recorded(self):
        """执行后记录已执行 SQL，可由 take_executed_sqls 取回。"""
        runtime = _runtime()
        self._call(runtime, sql="SELECT company_name FROM ads_trip.tbl WHERE pt_dt = '20260919'")
        records = take_executed_sqls("req-1")
        self.assertEqual(len(records), 1)
        self.assertIn("LIMIT 50", records[0]["sql"])

    def test_partition_fields_from_semantic(self):
        """明细表非 pt_dt 分区：从语义层取分区字段透传执行守卫。"""
        part_map = {"ads_trip.detail_tbl": ["create_time"]}
        runtime = _runtime(part_map=part_map)
        self._call(
            runtime,
            sql="SELECT detail FROM ads_trip.detail_tbl WHERE create_time >= '2026-01-01' AND create_time < '2026-02-01' LIMIT 5",
        )
        trino = runtime["tool_registry"].get_by_name("sql_query_trino").tool
        self.assertIn("create_time", trino.calls[0]["partition_fields"])
    def test_executed_sql_overwritten_by_latest_call(self):
        """后一次 execute_query 覆盖前一次：前端只展示最终生效 SQL（不展示中间纠错 SQL）。"""
        runtime = _runtime()
        self._call(runtime, sql="SELECT company_name FROM ads_trip.tbl WHERE pt_dt = '20260919'")
        self._call(runtime, sql="SELECT company_id, company_name FROM ads_trip.tbl WHERE pt_dt = '20260919' GROUP BY company_id, company_name")
        records = take_executed_sqls("req-1")
        self.assertEqual(len(records), 1)
        self.assertIn("GROUP BY", records[0]["sql"])

    def test_executed_sql_steps_keep_all_segments(self):
        """steps 多段：同一次 execute_query 调用的全部段保留（不被覆盖）。"""
        runtime = _runtime()
        steps = json.dumps([
            {"id": "s1", "sql": "SELECT COUNT(*) AS c FROM ads_trip.t1 WHERE pt_dt = '20260919'", "question": "指标1"},
            {"id": "s2", "sql": "SELECT COUNT(*) AS c FROM ads_trip.t2 WHERE pt_dt = '20260919'", "question": "指标2"},
        ])
        self._call(runtime, steps=steps)
        records = take_executed_sqls("req-1")
        self.assertEqual(len(records), 2)