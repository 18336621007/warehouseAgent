# 方案模板简化后：select_fields/filters 派生执行字段的回归测试
# 覆盖：detail 与聚合两种场景的派生、draft 校验、finalize 到 locked
import unittest

from agentTest.langgraph_app.services.query_plan_service import merge_draft_plan
from agentTest.langgraph_app.services.plan_synthesizer import finalize_draft_plan
from agentTest.langgraph_app.state.query_plan import validate_query_plan


class DraftPlanSimplificationTest(unittest.TestCase):
    """S3 方案模板简化：Advisor/Planner 只写 select_fields/filters，执行字段由程序派生"""

    def test_draft_detail_select_fields_derives_time_and_clears_execution(self):
        draft = merge_draft_plan({}, {
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "select_fields": ["goods_no", "company_name", "region_name"],
            "filters": "create_time 今年 AND region_name='徐州大区' AND status='同意返厂'",
            "detail_query": True,
        })
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["time_field"], "create_time")
        self.assertEqual(draft["time_range"], "今年")
        # 明细查询不聚合，measures/dimensions 均应为空
        self.assertEqual(draft.get("measures") or [], [])
        self.assertEqual(draft.get("dimensions") or [], [])
        self.assertIn("create_time", draft.get("fields") or [])
        # draft 结构校验通过
        self.assertEqual(validate_query_plan(draft), [])

    def test_detail_draft_finalize_to_locked(self):
        draft = merge_draft_plan({}, {
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "select_fields": ["goods_no", "company_name", "region_name"],
            "filters": "create_time 今年 AND region_name='徐州大区' AND status='同意返厂'",
            "detail_query": True,
        })
        locked = finalize_draft_plan(draft)
        self.assertIsNotNone(locked)
        self.assertEqual(locked["status"], "locked")
        self.assertTrue(locked.get("detail_query"))
        self.assertEqual(locked["time_field"], "create_time")
        self.assertEqual(validate_query_plan(locked, require_confirmed=False), [])

    def test_draft_aggregate_select_fields_derives_measures_dimensions(self):
        draft = merge_draft_plan({}, {
            "tables": [
                "ads_trip.ads_region_rent_order_analysis_hour",
                "dim_trip.dim_exchange_common_company_info_day",
            ],
            "select_fields": ["new_rent_counts", "company_id", "company_name"],
            "filters": "pt_dt 近7天 AND company_category='A'",
            "field_sources": [
                "ads_trip.ads_region_rent_order_analysis_hour.new_rent_counts",
                "ads_trip.ads_region_rent_order_analysis_hour.company_id",
                "dim_trip.dim_exchange_common_company_info_day.company_name",
            ],
        })
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["time_field"], "pt_dt")
        self.assertEqual(draft["time_range"], "近7天")
        # select_fields 应全部被拆到 measures/dimensions 中的任意一边
        covered = set((draft.get("measures") or []) + (draft.get("dimensions") or []))
        self.assertIn("new_rent_counts", covered)
        self.assertIn("company_id", covered)
        self.assertIn("company_name", covered)
        self.assertEqual(validate_query_plan(draft), [])

    def test_fallback_sql_supports_detail_select_fields(self):
        from agentTest.langgraph_app.nodes.generate_sql_node import _build_fallback_sql
        plan = {
            "detail_query": True,
            "table": "ads_trip.ads_gundam_device_return_detail_hour",
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "select_fields": ["goods_no", "company_name"],
            "time_field": "create_time",
            "time_range": "昨天",
            "filters": "region_name = '徐州大区'",
            "measures": [],
            "dimensions": [],
            "result_limit": 1000,
        }
        sql = _build_fallback_sql(plan)
        self.assertIn("SELECT goods_no, company_name", sql)
        self.assertIn("WHERE", sql)
        self.assertNotIn("GROUP BY", sql)

    def test_fallback_sql_rejects_detail_non_yesterday(self):
        # 明细查询非"昨天"时间范围不生成兜底 SQL，避免错误日期条件
        from agentTest.langgraph_app.nodes.generate_sql_node import _build_fallback_sql
        plan = {
            "detail_query": True,
            "table": "ads_trip.ads_gundam_device_return_detail_hour",
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "select_fields": ["goods_no"],
            "time_field": "create_time",
            "time_range": "今年",
            "measures": [],
            "dimensions": [],
        }
        self.assertEqual(_build_fallback_sql(plan), "")


if __name__ == "__main__":
    unittest.main()
