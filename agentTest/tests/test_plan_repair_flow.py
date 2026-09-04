# Planner 唯一路由 + Seeker 方案修复回退测试
# 覆盖：route_after_seeker 修复/兜底/结束分派、plan_error_fallback 用户提示、
#       plan_synthesizer 多指标确定性构建、草稿收尾
import unittest

from agentTest.config.advisor import MAX_PLAN_REPAIR_ROUNDS
from agentTest.langgraph_app.routers.seeker_router import route_after_seeker
from agentTest.langgraph_app.graphs.supervisor_graph import plan_error_fallback_node
from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
from agentTest.langgraph_app.services.plan_synthesizer import (
    build_plan_from_semantic,
    finalize_draft_plan,
)


class SeekerRepairRoutingTest(unittest.TestCase):
    """Seeker 方案不可行时回 Planner 修复的分派逻辑。"""

    def test_plan_error_within_budget_goes_repair(self):
        state = {"seeker_plan_error": "缺少 join 契约", "plan_repair_rounds": 0}
        self.assertEqual(route_after_seeker(state), "repair")

    def test_plan_error_budget_exhausted_goes_fallback(self):
        state = {
            "seeker_plan_error": "缺少 join 契约",
            "plan_repair_rounds": MAX_PLAN_REPAIR_ROUNDS,
        }
        self.assertEqual(route_after_seeker(state), "fallback")

    def test_success_goes_end(self):
        self.assertEqual(route_after_seeker({"seeker_plan_error": ""}), "end")
        self.assertEqual(route_after_seeker({}), "end")

    def test_fallback_node_returns_friendly_message(self):
        result = plan_error_fallback_node({
            "seeker_plan_error": "当前查询涉及多张表，但缺少必要的关联关系配置。",
            "request_id": "req123",
        })
        self.assertIn("缺少必要的关联关系配置", result["final_answer"])
        self.assertEqual(result["topic_status"], "completed")
        self.assertTrue(result["messages"])


class PlanSynthesizerTest(unittest.TestCase):
    """Planner 直达 Seeker 的确定性方案构建。"""

    def _provider(self):
        return SemanticMetadataProvider(get_semantic_layer_provider())

    def test_multi_metric_builds_plan(self):
        sl = get_semantic_layer_provider()
        sp = self._provider()
        hits = [
            sl.get_metric_by_id("addition_order_num"),
            sl.get_metric_by_id("renting_order_num"),
        ]
        plan = build_plan_from_semantic(hits, sp, dimension_mentions=[], time_range="昨天")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("status"), "locked")
        self.assertIn("new_rent_counts", plan.get("measures"))
        self.assertIn("rent_order_counts", plan.get("measures"))

    def test_complex_metric_returns_none(self):
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("renewal_rate")], sp,
            dimension_mentions=[], time_range="昨天",
        )
        # 分子分母复合表达式无法确定为单度量 → 交 Advisor
        self.assertIsNone(plan)

    def test_finalize_draft_valid(self):
        draft = {
            "status": "draft",
            "tables": [
                "ads_trip.ads_region_rent_order_analysis_hour",
                "dim_trip.dim_exchange_common_company_info_day",
            ],
            "measures": ["new_rent_counts"],
            "dimensions": ["company_id", "company_name"],
            "time_field": "pt_dt",
            "time_range": "昨天",
            "filters": "company_category='A'",
            "field_sources": [
                "ads_trip.ads_region_rent_order_analysis_hour.new_rent_counts",
                "ads_trip.ads_region_rent_order_analysis_hour.company_id",
                "dim_trip.dim_exchange_common_company_info_day.company_name",
            ],
            "result_limit": 1000,
            "complex": False,
        }
        plan = finalize_draft_plan(draft)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("status"), "locked")


if __name__ == "__main__":
    unittest.main()
