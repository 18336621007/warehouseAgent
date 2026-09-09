# -*- coding: utf-8 -*-
# dimension_mentions 软失败测试：未命中实体的维度词不再阻塞方案构建
# 覆盖：过滤值混入维度槽位（含 filters 值被截断）仍可构建；可解析实体维度照常解析；
#       unresolved_dimensions 写入方案供日志审计
import unittest

from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
from agentTest.langgraph_app.services.plan_synthesizer import build_plan_from_semantic


class DimensionMentionsSoftFailTest(unittest.TestCase):
    """Planner 直达 Seeker 的维度解析软失败行为。"""

    def _provider(self):
        return SemanticMetadataProvider(get_semantic_layer_provider())

    def test_filter_value_in_dimension_mentions_not_blocked(self):
        # 本次 bug 根因：过滤值"徐州大区"混入 dimension_mentions 且 filters 值被截断为"徐州"，
        # 旧逻辑 `word in filters` 字符串包含判断失效 → 构建失败降级循环；现改为软失败跳过
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("addition_order_num")], sp,
            dimension_mentions=["徐州大区"],
            filters="region_name = '徐州'",
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("status"), "confirmed")
        self.assertEqual(plan.get("unresolved_dimensions"), ["徐州大区"])

    def test_resolvable_entity_still_resolves(self):
        # 可解析实体（经销商）行为不变：正常解析为分组字段
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("addition_order_num")], sp,
            dimension_mentions=["经销商"],
            filters="region_name = '徐州大区'",
        )
        self.assertIsNotNone(plan)
        self.assertIn("company_id", plan.get("dimensions") or [])
        self.assertNotIn("unresolved_dimensions", plan)

    def test_mixed_resolvable_and_unresolvable_words(self):
        # 同一批词中部分可解析、部分不可解析：可解析的照常入维度，不可解析的进 unresolved
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("addition_order_num")], sp,
            dimension_mentions=["经销商", "徐州大区"],
            filters="region_name = '徐州大区'",
        )
        self.assertIsNotNone(plan)
        self.assertIn("company_id", plan.get("dimensions") or [])
        self.assertEqual(plan.get("unresolved_dimensions"), ["徐州大区"])

    def test_empty_dimension_mentions_builds_plan(self):
        # 空维度槽位基线：不产生 unresolved 字段
        sl = get_semantic_layer_provider()
        sp = self._provider()
        plan = build_plan_from_semantic(
            [sl.get_metric_by_id("addition_order_num")], sp,
            dimension_mentions=[],
            filters="region_name = '徐州大区'",
        )
        self.assertIsNotNone(plan)
        self.assertNotIn("unresolved_dimensions", plan)


if __name__ == "__main__":
    unittest.main()
