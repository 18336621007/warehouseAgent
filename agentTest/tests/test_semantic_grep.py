# 语义层全文 grep + 指针导航测试（第0-2层，对齐 skill 关键词 grep 思路）
# 覆盖：notes/definition 命中、强弱命中区分、指针导航、format_metric_context 兼容
import unittest

from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.semantic_layer.metric_matcher import (
    grep_metrics_from_keywords,
    resolve_metric_chain,
    format_metric_context,
)


class SemanticGrepTest(unittest.TestCase):
    """语义层 grep 检索与指针导航测试。"""

    def test_grep_hits_transfer_detail_via_notes(self):
        """"调出" 只出现在 device_transfer_detail 的 notes 中，grep 应能命中（词法 match 漏召）。"""
        hits = grep_metrics_from_keywords(["调出", "明细"], limit=10)
        ids = [m["id"] for m in hits]
        self.assertIn("device_transfer_detail", ids)
        top = hits[0]
        self.assertEqual(top["id"], "device_transfer_detail")
        # "明细"在名称中 → 强命中
        self.assertEqual(top["hit_type"], "strong")
        self.assertIn("调出", top.get("weak_hits") or [])
        self.assertIn("明细", top.get("strong_hits") or [])

    def test_grep_weak_hit_type(self):
        """只有 notes/definition 命中时标记为 weak。"""
        hits = grep_metrics_from_keywords(["调出未签收"], limit=10)
        # "调出未签收" 出现在 haerbin 指标 notes 中
        self.assertTrue(hits)
        self.assertTrue(all(m["hit_type"] == "weak" for m in hits))

    def test_grep_strong_hit_ranks_higher(self):
        """名称/别名命中的强命中应排在仅备注弱命中的前面。"""
        strong = grep_metrics_from_keywords(["新增订单"], limit=5)
        self.assertTrue(strong)
        self.assertEqual(strong[0]["hit_type"], "strong")

    def test_resolve_metric_chain(self):
        """指针导航：metric → semantic_model → physical，应取到物理表信息。"""
        chain = resolve_metric_chain("device_transfer_detail")
        self.assertIn("metric", chain)
        self.assertIn("semantic_model", chain)
        self.assertIn("physical", chain)
        self.assertEqual(chain["metric"]["id"], "device_transfer_detail")
        self.assertEqual(
            chain["semantic_model"]["id"],
            "ads_trip.ads_gundam_device_transfer_detail_hour",
        )
        self.assertEqual(
            chain["physical"]["full_name"],
            "ads_trip.ads_gundam_device_transfer_detail_hour",
        )
        # 无分区明细表：物理表 partition 为空，供 SQL 生成判断用日期字段过滤
        self.assertEqual(chain["physical"]["partition"], [])

    def test_format_metric_context_handles_grep_hits(self):
        """format_metric_context 应能渲染 grep 命中（含非字符串 notes 的兼容）。"""
        hits = grep_metrics_from_keywords(["调出", "明细"], limit=3)
        text = format_metric_context(hits)
        self.assertIn("调货明细", text)
        self.assertIn("device_transfer_detail", text)
        self.assertIn("命中: strong", text)

    def test_provider_grep_empty_keywords(self):
        """空关键词返回空列表，不报错。"""
        provider = get_semantic_layer_provider()
        self.assertEqual(provider.grep_metrics([], limit=5), [])
        self.assertEqual(provider.grep_metrics(None, limit=5), [])


if __name__ == "__main__":
    unittest.main()
