# generate_sql 确定性 SQL 翻译测试：方案完整时程序化构造标准 SQL（跳过 LLM），多段查询不再每段重新思考
# 覆盖：SUM 单字段型指标走确定性翻译（LLM 0 调用）；过滤字段归属稳定（pt_dt 不误归扩展维表）
import unittest
from unittest import mock

from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
from agentTest.langgraph_app.services.plan_synthesizer import build_plan_from_semantic
from agentTest.langgraph_app.nodes.generate_sql_node import (
    build_generate_sql_node,
    _validate_sql_against_plan,
)


def _runtime(llm):
    return {
        "llm": llm,
        "prompt": mock.MagicMock(),
        "example_vector_store": None,
    }


class GenerateSqlDeterministicTest(unittest.TestCase):
    """确定性 SQL 翻译：方案完整时 0 LLM 调用直接出 SQL。"""

    def setUp(self):
        self.provider = SemanticMetadataProvider(get_semantic_layer_provider())

    def _same_table_hits(self):
        hits = []
        for mid in ["renting_order_num", "month_renting_order_num", "overdue_gt30_order_num", "stag_order_num"]:
            m = self.provider.semantic_layer.get_metric_by_id(mid)
            if m:
                hits.append(m)
        return hits

    def test_sum_metric_uses_deterministic_translation(self):
        """SUM 单字段型指标：确定性翻译直接出 SQL，LLM 不应被调用。"""
        plan = build_plan_from_semantic(
            metric_hits=self._same_table_hits(),
            semantic_provider=self.provider,
            filters="pt_dt = '2026-09-15'",
        )
        self.assertIsNotNone(plan)
        # 同表 4 指标合并后 plan 应保留语义层聚合表达式
        self.assertEqual(
            plan.get("measure_expressions", {}).get("rent_order_counts"),
            "SUM(rent_order_counts)",
        )
        llm = mock.MagicMock(side_effect=AssertionError("确定性翻译不应调用 LLM"))
        node = build_generate_sql_node(_runtime(llm))
        state = {
            "confirmed_plan": plan,
            "effective_query": "昨天租赁中、月租、逾期>30天、滞纳订单数",
            "schema_context": "",
            "messages": [],
            "retry_count": 0,
            "sql_fix_reason": "",
            "generated_sql": "",
        }
        out = node(state)
        sql = out.get("generated_sql") or ""
        self.assertTrue(sql.startswith("SELECT"))
        self.assertIn("rent_order_counts", sql)
        self.assertEqual(out.get("topic_status"), "validating_sql")
        # 确定性翻译的 SQL 必须通过程序化校验（否则不应拦截 LLM 路径）
        self.assertEqual(_validate_sql_against_plan(sql, plan), [])

    def test_filter_field_owner_stable(self):
        """过滤字段归属稳定：pt_dt 优先归主表，tables 不含误归的扩展维表。"""
        plan = build_plan_from_semantic(
            metric_hits=self._same_table_hits(),
            semantic_provider=self.provider,
            filters="pt_dt = '2026-09-15'",
        )
        self.assertIsNotNone(plan)
        tables = plan.get("tables") or []
        # 同表 4 指标只应落在主表，不因 pt_dt 被维表抢占而引入无关联维表
        self.assertEqual(tables, ["ads_trip.ads_region_rent_order_analysis_hour"])
        self.assertEqual(plan.get("field_sources", {}).get("pt_dt"), "ads_trip.ads_region_rent_order_analysis_hour")

    def test_measure_expressions_passthrough_locked_plan(self):
        """measure_expressions 应经过 lock_query_plan 透传保留，供 generate_sql 确定性翻译复用。"""
        plan = build_plan_from_semantic(
            metric_hits=self._same_table_hits(),
            semantic_provider=self.provider,
            filters="pt_dt = '2026-09-15'",
        )
        self.assertIsNotNone(plan)
        exprs = plan.get("measure_expressions") or {}
        for mid, field in [
            ("renting_order_num", "rent_order_counts"),
            ("month_renting_order_num", "month_renting_order_counts"),
            ("overdue_gt30_order_num", "overdue30_days_rents"),
            ("stag_order_num", "stag_order_counts"),
        ]:
            self.assertEqual(exprs.get(field), f"SUM({field})")


if __name__ == "__main__":
    unittest.main()
