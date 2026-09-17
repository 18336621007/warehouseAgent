# 多数据源引擎路由单元测试
# 覆盖：datasource_scope 候选解析 / 注册表行为 / execute_sql_node 引擎路由与降级
import unittest

from agentTest.datasource.registry import DataSourceRegistry, resolve_engine_candidates
from agentTest.langgraph_app.nodes.execute_sql_node import build_execute_sql_node
from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec


class _StubDataSource:
    """数据源替身：只记录查询调用，不连真实引擎。"""

    engine = ""

    def __init__(self, name, error=None):
        self.engine = name
        self.error = error
        self.calls = 0

    def query(self, sql, timeout_seconds=None, max_rows=None):
        self.calls += 1
        if self.error:
            raise RuntimeError(f"{self.engine} 执行失败")
        return {"sql": sql, "columns": ["c"], "rows": [[1]], "row_count": 1}


class _StubSqlTool:
    """SQL 工具替身：invoke 转发到底层 datasource（执行异常抛 RuntimeError）。"""

    def __init__(self, name, datasource):
        self.name = name
        self.description = name
        self.datasource = datasource

    def invoke(self, args):
        return self.datasource.query(args.get("sql"))


def _build_node(engine_errors=None):
    """构造带 stub 引擎工具的 execute_sql_node，engine_errors 指定哪些引擎抛错。"""
    engine_errors = engine_errors or {}
    registry = ToolRegistry()
    for engine in ("trino", "hive", "doris"):
        ds = _StubDataSource(engine, error=engine_errors.get(engine))
        registry.register(ToolSpec(
            name=f"sql_query_{engine}",
            description=f"sql {engine}",
            tool=_StubSqlTool(f"sql_query_{engine}", ds),
        ))
    runtime = {"tool_registry": registry}
    return build_execute_sql_node(runtime)


class EngineScopeResolveTest(unittest.TestCase):
    """datasource_scope 候选链解析。"""

    def test_data_project_to_doris(self):
        self.assertEqual(
            resolve_engine_candidates("data_project.ads_gundam_boss_meituan_renting_day"),
            ["doris"],
        )

    def test_others_trino_hive(self):
        self.assertEqual(
            resolve_engine_candidates("ads_trip.ads_exchange_platform_operations_report_day"),
            ["trino", "hive"],
        )

    def test_default_fallback(self):
        self.assertEqual(resolve_engine_candidates("unknown_db.some_table"), ["trino", "hive"])


class DataSourceRegistryTest(unittest.TestCase):
    """注册表注册/获取/候选过滤。"""

    def test_register_and_get(self):
        reg = DataSourceRegistry()
        reg.register("hive", _StubDataSource("hive"))
        reg.register("doris", _StubDataSource("doris"))
        self.assertEqual(reg.engines(), ["hive", "doris"])
        self.assertIsNotNone(reg.get_datasource("hive"))
        self.assertIsNone(reg.get_dataspace("trino") if False else reg.get_datasource("trino"))

    def test_get_candidates_filters_unregistered(self):
        reg = DataSourceRegistry()
        reg.register("hive", _StubDataSource("hive"))
        # data_project -> [doris] 但 doris 未注册：应返回空列表
        self.assertEqual(reg.get_candidates("data_project.xxx"), [])
        # 其他表 -> [trino, hive]，仅 hive 已注册
        self.assertEqual(reg.get_candidates("ads_trip.xxx"), ["hive"])


class ExecuteSqlNodeEngineTest(unittest.TestCase):
    """execute_sql_node 引擎路由与降级。"""

    def _plan(self, candidates, cross_engine=False):
        return {
            "tables": ["data_project.ads_gundam_boss_meituan_renting_day"],
            "measures": ["renting_order_counts"],
            "dimensions": [],
            "select_fields": ["renting_order_counts"],
            "time_field": "pt_dt",
            "filters": "pt_dt='20260915'",
            "engine_candidates": candidates,
            "cross_engine": cross_engine,
        }

    def test_prefer_first_engine(self):
        node = _build_node()
        r = node({"generated_sql": "SELECT 1", "confirmed_plan": self._plan(["doris"])})
        self.assertFalse(r["sql_exec_failed"])
        self.assertEqual(r["sql_result"]["row_count"], 1)

    def test_fallback_to_next_engine(self):
        # trino 失败 -> 降级 hive 成功
        node = _build_node(engine_errors={"trino": "boom"})
        r = node({"generated_sql": "SELECT 1", "confirmed_plan": self._plan(["trino", "hive"])})
        self.assertFalse(r["sql_exec_failed"])

    def test_all_engines_failed_summary(self):
        node = _build_node(engine_errors={"trino": "boom", "hive": "boom"})
        r = node({"generated_sql": "SELECT 1", "confirmed_plan": self._plan(["trino", "hive"])})
        self.assertTrue(r["sql_exec_failed"])
        self.assertIn("trino", r["sql_exec_error"])
        self.assertIn("hive", r["sql_exec_error"])

    def test_cross_engine_rejected(self):
        node = _build_node()
        r = node({"generated_sql": "SELECT 1", "confirmed_plan": self._plan(["doris"], cross_engine=True)})
        self.assertTrue(r["sql_exec_failed"])
        self.assertIn("跨数据源", r["sql_exec_error"])


if __name__ == "__main__":
    unittest.main()
