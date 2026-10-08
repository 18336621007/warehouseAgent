# 多数据源引擎路由单元测试
# 覆盖：datasource_scope 候选解析 / 注册表注册与候选过滤
import unittest

from agentTest.datasource.registry import DataSourceRegistry, resolve_engine_candidates


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


if __name__ == "__main__":
    unittest.main()
