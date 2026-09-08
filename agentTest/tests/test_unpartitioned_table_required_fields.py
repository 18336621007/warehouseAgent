# 无分区表必选过滤字段回归测试：
# 有 pt_dt 分区的表仍强制 pt_dt；无 pt_dt 分区（无分区表）改用方案业务时间字段，
# 避免聚合场景被一致性修复强制加 pt_dt 导致引用不存在的列。
import unittest

from agentTest.langgraph_app.nodes.generate_sql_node import _detail_required_fields
from agentTest.langgraph_app.nodes.generate_sql_node import _validate_sql_against_plan
from agentTest.langgraph_app.services.sql_table_filter_validator import resolve_required_filter_fields
from agentTest.langgraph_app.services.sql_table_filter_validator import validate_table_plan_filters

# 无分区表：返厂明细（无 pt_dt 列，时间字段为 create_time）
UNPARTITIONED_TABLE = "ads_trip.ads_gundam_device_return_detail_hour"
# 有 pt_dt 分区表：平台运营日报
PARTITIONED_TABLE = "ads_trip.ads_exchange_platform_operations_report_day"


def _build_aggregate_plan(table: str, time_field: str) -> dict:
    """构造与报错场景一致的聚合方案。"""
    return {
        "table": table,
        "tables": [table],
        "measures": [],
        "dimensions": ["disable_type"],
        "time_field": time_field,
        "time_range": "2026-01-01 至 2026-09-07",
        "filters": (
            "region_name='徐州大区' AND create_time >= '2026-01-01' "
            "AND create_time <= '2026-09-07' AND status='同意返厂'"
        ),
        "table_plans": [
            {
                "table": table,
                "time_field": time_field,
                "time_range": "2026-01-01 至 2026-09-07",
                "filters": "region_name='徐州大区' AND status='同意返厂'",
            }
        ],
    }


class UnpartitionedTableRequiredFieldsTest(unittest.TestCase):
    """必选过滤字段应跟随表的分区情况，而非写死 pt_dt。"""

    def test_aggregate_unpartitioned_uses_plan_time_field(self):
        """无分区表上的聚合查询：必选过滤字段应使用方案业务时间字段。"""
        plan = _build_aggregate_plan(UNPARTITIONED_TABLE, "create_time")
        self.assertEqual(_detail_required_fields(plan), ["create_time"])
        self.assertEqual(resolve_required_filter_fields(plan), ["create_time"])

    def test_detail_unpartitioned_uses_plan_time_field(self):
        """无分区表上的明细查询：同样使用方案业务时间字段。"""
        plan = _build_aggregate_plan(UNPARTITIONED_TABLE, "create_time")
        plan["detail_query"] = True
        plan["measures"] = []
        plan["dimensions"] = []
        self.assertEqual(_detail_required_fields(plan), ["create_time"])

    def test_partitioned_table_still_requires_pt_dt(self):
        """有 pt_dt 分区的表：仍强制 pt_dt，行为不变。"""
        plan = _build_aggregate_plan(PARTITIONED_TABLE, "pt_dt")
        self.assertEqual(_detail_required_fields(plan), ["pt_dt"])

    def test_unknown_table_keeps_default_pt_dt(self):
        """表不在语义层（RAG 兜底）：保持默认 pt_dt，行为不变。"""
        plan = _build_aggregate_plan("ads_trip.no_such_table", "create_time")
        self.assertEqual(_detail_required_fields(plan), ["pt_dt"])

    def test_validate_accepts_business_time_filter_on_unpartitioned(self):
        """无分区表聚合 SQL 使用 create_time 过滤：应通过逐表过滤校验。"""
        plan = _build_aggregate_plan(UNPARTITIONED_TABLE, "create_time")
        sql = (
            "SELECT disable_type, COUNT(*) AS cnt FROM ads_trip.ads_gundam_device_return_detail_hour "
            "WHERE region_name = '徐州大区' AND status = '同意返厂' "
            "AND create_time >= '2026-01-01' AND create_time <= '2026-09-07' "
            "GROUP BY disable_type LIMIT 50"
        )
        self.assertEqual(validate_table_plan_filters(
            sql,
            plan["tables"],
            plan["table_plans"],
            required_filter_fields=_detail_required_fields(plan),
        ), [])

    def test_validate_rejects_missing_business_time_on_unpartitioned(self):
        """无分区表聚合 SQL 只带 pt_dt、缺少 create_time：必须报错，禁止无业务时间过滤。"""
        plan = _build_aggregate_plan(UNPARTITIONED_TABLE, "create_time")
        sql = (
            "SELECT disable_type, COUNT(*) AS cnt FROM ads_trip.ads_gundam_device_return_detail_hour "
            "WHERE pt_dt >= '2026-01-01' AND pt_dt <= '2026-09-07' "
            "AND region_name = '徐州大区' GROUP BY disable_type LIMIT 50"
        )
        issues = validate_table_plan_filters(
            sql,
            plan["tables"],
            plan["table_plans"],
            required_filter_fields=_detail_required_fields(plan),
        )
        self.assertTrue(any("create_time" in issue for issue in issues), issues)

    def test_validate_against_plan_accepts_aggregate_sql_with_create_time(self):
        """完整一致性校验：本次报错场景的原始 SQL 应直接通过，不再被强制加 pt_dt。"""
        plan = _build_aggregate_plan(UNPARTITIONED_TABLE, "create_time")
        sql = (
            "SELECT disable_type, COUNT(*) AS cnt FROM ads_trip.ads_gundam_device_return_detail_hour "
            "WHERE region_name = '徐州大区' AND status = '同意返厂' "
            "AND create_time >= '2026-01-01' AND create_time <= '2026-09-07' "
            "GROUP BY disable_type LIMIT 50"
        )
        self.assertEqual(_validate_sql_against_plan(sql, plan), [])

    def test_partitioned_table_sql_missing_pt_dt_still_rejected(self):
        """有 pt_dt 分区的表 SQL 缺 pt_dt：仍应被逐表过滤校验拦截。"""
        plan = {
            "table": PARTITIONED_TABLE,
            "tables": [PARTITIONED_TABLE],
            "measures": ["new_order"],
            "dimensions": ["company_name"],
            "time_field": "pt_dt",
            "time_range": "昨天",
            "filters": "company_category='A'",
            "table_plans": [
                {
                    "table": PARTITIONED_TABLE,
                    "time_field": "pt_dt",
                    "time_range": "昨天",
                    "filters": "company_category='A'",
                }
            ],
        }
        sql = (
            "SELECT company_name, SUM(new_order) AS new_order "
            "FROM ads_trip.ads_exchange_platform_operations_report_day "
            "WHERE company_category='A' GROUP BY company_name LIMIT 50"
        )
        issues = validate_table_plan_filters(
            sql,
            plan["tables"],
            plan["table_plans"],
            required_filter_fields=_detail_required_fields(plan),
        )
        self.assertTrue(any("pt_dt" in issue for issue in issues), issues)


if __name__ == "__main__":
    unittest.main()
