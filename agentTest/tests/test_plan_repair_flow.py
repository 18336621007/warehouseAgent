# Planner 唯一路由 + Seeker 方案修复回退测试
# 覆盖：route_after_seeker 修复/兜底/结束分派、plan_error_fallback 用户提示、
#       plan_synthesizer 多指标确定性构建、草稿收尾
import unittest

from agentTest.config.advisor import MAX_PLAN_REPAIR_ROUNDS, MAX_ADVISOR_AUTO_CONTINUE
from agentTest.langgraph_app.routers.seeker_router import route_after_seeker, route_after_advisor
from agentTest.langgraph_app.graphs.supervisor_graph import plan_error_fallback_node
from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
from agentTest.langgraph_app.services.plan_synthesizer import (
    build_plan_from_semantic,
    finalize_draft_plan,
)
from agentTest.langgraph_app.state.query_plan import validate_query_plan


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

    def test_unresolvable_plan_error_skips_repair(self):
        # 缺 join 契约等不可修复错误：即使修复轮次未耗尽也直接走 fallback 告知用户
        state = {
            "seeker_plan_error": "缺少 join 契约",
            "seeker_error_unresolvable": True,
            "plan_repair_rounds": 0,
        }
        self.assertEqual(route_after_seeker(state), "fallback")


class AdvisorAutoContinueRoutingTest(unittest.TestCase):
    """Advisor 收尾结构化 next_step 决定是否自动回 Planner 的分派逻辑。"""

    def test_return_to_planner_goes_planner(self):
        state = {
            "advisor_next_step": "return_to_planner",
            "advisor_auto_rounds": 1,
        }
        self.assertEqual(route_after_advisor(state), "planner")

    def test_wait_user_goes_end(self):
        # 收尾要求等用户回复：即使文本没有问号也不自动回 Planner
        state = {
            "advisor_next_step": "wait_user",
            "advisor_auto_rounds": 1,
        }
        self.assertEqual(route_after_advisor(state), "end")

    def test_missing_next_step_goes_end(self):
        # 没有结构化收尾结果（异常兜底/旧状态）时保守等用户
        self.assertEqual(route_after_advisor({"advisor_next_step": None}), "end")
        self.assertEqual(route_after_advisor({}), "end")

    def test_auto_continue_budget_exhausted_goes_end(self):
        state = {
            "advisor_next_step": "return_to_planner",
            "advisor_auto_rounds": MAX_ADVISOR_AUTO_CONTINUE + 1,
        }
        self.assertEqual(route_after_advisor(state), "end")


class PlanErrorFallbackTest(unittest.TestCase):
    """Seeker 方案不可行且修复机会耗尽时给用户的兜底回复。"""

    def test_fallback_node_returns_friendly_message(self):
        result = plan_error_fallback_node({
            "seeker_plan_error": "当前查询涉及多张表，但缺少必要的关联关系配置。",
            "request_id": "req123",
        })
        self.assertIn("缺少必要的关联关系配置", result["final_answer"])
        self.assertEqual(result["topic_status"], "completed")
        self.assertTrue(result["messages"])

    def test_fallback_node_unresolvable_mentions_admin(self):
        # 缺 join 契约：最终答复明确告知用户无法关联、请联系管理员
        result = plan_error_fallback_node({
            "seeker_plan_error": "当前查询涉及多张表，但缺少必要的关联关系配置。",
            "seeker_error_unresolvable": True,
            "request_id": "req124",
        })
        self.assertIn("请联系数据管理员", result["final_answer"])
        self.assertIn("缺少关联关系配置", result["final_answer"])
        self.assertEqual(result["topic_status"], "completed")


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

    def test_detail_metric_with_draft_builds_plan(self):
        # 返厂明细：明细型指标 + advisor 已确认 create_time 草稿 → 可直达 Seeker
        sl = get_semantic_layer_provider()
        sp = self._provider()
        draft = {
            "status": "draft",
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "measures": [],
            "dimensions": [],
            "time_field": "create_time",
            "time_range": "今年",
            "filters": "region_name = '徐州大区' AND status = '同意返厂'",
            "field_sources": [],
            "result_limit": 1000,
            "complex": False,
        }
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("device_return_detail")], sp,
            dimension_mentions=[], time_range="今年",
            filters="region_name = '徐州大区' AND status = '同意返厂'",
            draft=draft,
        )
        self.assertIsNotNone(plan)
        self.assertTrue(plan.get("detail_query"))
        self.assertEqual(plan.get("time_field"), "create_time")
        self.assertEqual(plan.get("measures"), [])

    def test_detail_metric_without_draft_returns_none(self):
        # 无分区明细表且无草稿时间字段时禁止回退 pt_dt，交 Advisor 澄清日期字段
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("device_return_detail")], sp,
            dimension_mentions=[], time_range="今年",
        )
        self.assertIsNone(plan)

    def test_finalize_detail_draft(self):
        # 无度量无维度分组的草稿视为明细查询，可收尾锁定
        draft = {
            "status": "draft",
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "measures": [],
            "dimensions": [],
            "time_field": "create_time",
            "time_range": "今年",
            "filters": "region_name = '徐州大区' AND status = '同意返厂'",
            "field_sources": [],
            "result_limit": 1000,
            "complex": False,
        }
        plan = finalize_draft_plan(draft)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("status"), "locked")
        self.assertTrue(plan.get("detail_query"))

    def test_locked_detail_plan_passes_seeker_validation(self):
        # Planner 直接构建的 locked 明细方案应通过 Seeker 入口校验（无用户确认环节）
        locked = {
            "status": "locked",
            "table": "ads_trip.ads_gundam_device_return_detail_hour",
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "measures": [],
            "dimensions": [],
            "time_field": "create_time",
            "time_range": "今年",
            "filters": "region_name = '徐州大区' AND status = '同意返厂'",
            "fields": ["create_time", "region_name", "status"],
            "field_sources": {},
            "detail_query": True,
            "table_plans": [
                {
                    "table": "ads_trip.ads_gundam_device_return_detail_hour",
                    "time_field": "create_time",
                    "time_range": "今年",
                    "filters": "region_name = '徐州大区' AND status = '同意返厂'",
                }
            ],
            "result_limit": 1000,
            "complex": False,
            "order_by": [],
            "having": "",
        }
        errors = validate_query_plan(locked, require_confirmed=False)
        self.assertEqual(errors, [])

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
