# S0-B 回归：fields 空但 tables/filters 有值的最小方案应能通过 lock_query_plan（不再降级 Advisor）
import unittest

from agentTest.langgraph_app.nodes.planner_node import _build_minimal_plan
from agentTest.langgraph_app.state.query_plan import validate_query_plan


class _FakeOutput:
    """构造 PlannerOutput 的轻量替身，仅暴露 _build_minimal_plan 所需字段。"""

    def __init__(self, tables, fields, filters, analysis_type):
        self.tables = tables
        self.fields = fields
        self.filters = filters
        self.analysis_type = analysis_type


class TestMinimalPlanEmptyFields(unittest.TestCase):
    """fields 空但表与过滤条件齐备时，minimal 方案不再校验失败。"""

    def test_empty_fields_with_table_and_filter(self):
        out = _FakeOutput(
            tables=["ads_trip.ads_exchange_platform_operations_report_day"],
            fields=[],
            filters="pt_date = '2026-09-09'",
            analysis_type="aggregation",
        )
        plan = _build_minimal_plan(out)
        self.assertIsNotNone(plan)
        # 从 filters 推导时间字段与范围，strict 校验通过
        self.assertEqual(validate_query_plan(plan, require_confirmed=True), [])

    def test_no_tables_returns_none(self):
        out = _FakeOutput(
            tables=[],
            fields=[],
            filters="pt_date = '2026-09-09'",
            analysis_type="aggregation",
        )
        self.assertIsNone(_build_minimal_plan(out))

    def test_detail_without_time_field_returns_none(self):
        # 明细查询且 filters 无时间字段：不回退 pt_dt，strict 校验拦截 → None（避免无分区明细误用）
        out = _FakeOutput(
            tables=["ads_trip.ads_exchange_platform_operations_report_day"],
            fields=[],
            filters="region_name = '徐州大区'",
            analysis_type="detail",
        )
        self.assertIsNone(_build_minimal_plan(out))


if __name__ == "__main__":
    unittest.main()
