# Planner 唯一决策 + 执行链方案修复回退测试（A1 路由收敛 execute/respond）
# 覆盖：route_after_seeker 修复/兜底/回看/结束分派、plan_error_fallback 用户提示、
#       plan_synthesizer 多指标确定性构建、草稿收尾
import unittest

from agentTest.config.advisor import MAX_PLAN_REPAIR_ROUNDS
from agentTest.langgraph_app.routers.seeker_router import route_after_seeker
from agentTest.langgraph_app.graphs.supervisor_graph import plan_error_fallback_node
from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
from agentTest.langgraph_app.services.plan_synthesizer import (
    build_plan_from_semantic,
)
from agentTest.langgraph_app.services.query_plan_service import lock_query_plan
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

    def test_execution_review_goes_planner(self):
        # A1：执行成功且有结果 → 回 Planner 基于落盘结果撰写最终回答
        state = {"execution_review": True, "seeker_empty_result": False}
        self.assertEqual(route_after_seeker(state), "review")

    def test_empty_result_goes_empty_self_heal(self):
        # 0 行自愈：执行成功但无数据 → 回 Planner 用 probe_values 核实
        state = {"seeker_empty_result": True, "execution_review": False}
        self.assertEqual(route_after_seeker(state), "empty_self_heal")

    def test_unresolvable_plan_error_skips_repair(self):
        # 缺 join 契约等不可修复错误：即使修复轮次未耗尽也直接走 fallback 告知用户
        state = {
            "seeker_plan_error": "缺少 join 契约",
            "seeker_error_unresolvable": True,
            "plan_repair_rounds": 0,
        }
        self.assertEqual(route_after_seeker(state), "fallback")


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
        self.assertEqual(plan.get("status"), "confirmed")
        self.assertIn("new_rent_counts", plan.get("measures"))
        self.assertIn("rent_order_counts", plan.get("measures"))

    def test_complex_metric_returns_none(self):
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("renewal_rate")], sp,
            dimension_mentions=[], time_range="昨天",
        )
        # 分子分母复合表达式无法确定为单度量 → Planner respond 澄清
        self.assertIsNone(plan)

    def test_detail_metric_with_draft_builds_plan(self):
        # 返厂明细：明细型指标 + 已确认 create_time 时间字段 → 可直达执行链
        sl = get_semantic_layer_provider()
        sp = self._provider()
        draft = {
            "status": "confirmed",
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
        # 无分区明细表且无时间字段时禁止回退 pt_dt，由 Planner respond 澄清日期
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("device_return_detail")], sp,
            dimension_mentions=[], time_range="今年",
        )
        self.assertIsNone(plan)

    def test_shared_detail_draft_locks(self):
        # 无度量无维度分组的共享方案视为明细查询，lock 后进入 Seeker
        draft = {
            "status": "confirmed",
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "measures": [],
            "dimensions": [],
            "time_field": "create_time",
            "time_range": "今年",
            "filters": "region_name = '徐州大区' AND status = '同意返厂'",
            "field_sources": [],
            "detail_query": True,
            "result_limit": 1000,
            "complex": False,
        }
        plan = lock_query_plan(draft)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("status"), "confirmed")
        self.assertTrue(plan.get("detail_query"))

    def test_locked_detail_plan_passes_seeker_validation(self):
        # Planner 直接构建的 locked 明细方案应通过 Seeker 入口校验（无用户确认环节）
        locked = {
            "status": "confirmed",
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
        errors = validate_query_plan(locked, require_confirmed=True)
        self.assertEqual(errors, [])

    def test_shared_draft_lock_valid(self):
        draft = {
            "status": "confirmed",
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
        plan = lock_query_plan(draft)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("status"), "confirmed")


    def test_detail_metric_with_filter_time_field_builds_plan(self):
        # 无分区明细表：filters 中带 create_time 时间条件（口径权威）时可直接构建
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("device_return_detail")], sp,
            dimension_mentions=[], time_range="今年",
            filters="region_name='徐州大区' AND status='同意返厂' AND create_time >= '2026-01-01' AND create_time <= '2026-12-31'",
        )
        self.assertIsNotNone(plan)
        self.assertTrue(plan.get("detail_query"))
        self.assertEqual(plan.get("time_field"), "create_time")
        self.assertEqual(plan.get("time_range"), "2026-01-01 至 2026-12-31")
        self.assertEqual(plan.get("status"), "confirmed")

    def test_detail_metric_without_time_field_returns_none(self):
        # 无分区明细表且 filters 未带时间条件时无法构建方案（禁止回退默认 pt_dt），交由 Planner respond 澄清
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("device_return_detail")], sp,
            dimension_mentions=[], time_range="今年",
            filters="region_name='徐州大区' AND status='同意返厂'",
        )
        self.assertIsNone(plan)


class SeekerDirectFlowTest(unittest.TestCase):
    """Planner 判定 seeker 但语义层未命中时，用 Planner 输出直通 Seeker 的回归测试。"""

    def test_minimal_plan_from_planner_output(self):
        # 本次 bug 场景：返厂原因聚合，语义层未命中，用 Planner 输出构造最小方案
        from types import SimpleNamespace
        from agentTest.langgraph_app.nodes.planner_node import _build_minimal_plan

        planner_output = SimpleNamespace(
            tables=["ads_trip.ads_gundam_device_return_detail_hour"],
            fields=["disable_type", "create_time", "count(*)"],
            filters="create_time >= '2026-01-01' AND create_time <= '2026-12-31'",
            analysis_type="aggregation",
        )
        plan = _build_minimal_plan(planner_output)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("status"), "confirmed")
        # count(*) 与时间字段不得作为分组维度，时间字段从 filters 派生
        self.assertNotIn("count(*)", plan.get("dimensions") or [])
        self.assertNotIn("create_time", plan.get("dimensions") or [])
        self.assertIn("disable_type", plan.get("dimensions") or [])
        self.assertEqual(plan.get("time_field"), "create_time")
        self.assertTrue(plan.get("table_plans"))

    def test_minimal_plan_requires_tables(self):
        # 完全没有表信息时返回 None，交由 Planner respond 澄清
        from types import SimpleNamespace
        from agentTest.langgraph_app.nodes.planner_node import _build_minimal_plan

        planner_output = SimpleNamespace(tables=[], fields=[], filters="", analysis_type="aggregation")
        self.assertIsNone(_build_minimal_plan(planner_output))


if __name__ == "__main__":
    unittest.main()
