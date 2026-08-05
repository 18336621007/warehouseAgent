# 第12课：AnalysisSpec 与指标口径歧义门禁测试。
# 覆盖：多候选拦截、历史案例不能确认口径、用户选择后同轮锁定、唯一候选直锁、伪造解析证据拒绝。
import unittest

from agentTest.langgraph_app.services.metric_ambiguity_validator import MetricAmbiguityValidator
from agentTest.langgraph_app.graphs.advisor_graph import _build_ambiguity_clarification
from agentTest.langgraph_app.services.query_plan_service import lock_query_plan

BASE_TABLE = "ads_trip.ads_exchange_platform_operations_report_day"


def _metric_candidates() -> list[dict]:
    """构造与真实日志一致的“新增订单”多候选字段列表。"""
    return [
        {"table": BASE_TABLE, "field": "new_order", "semantic_type": "measure",
         "comment": "全量新增订单数", "aliases": ["新增订单"], "score": 0.90},
        {"table": BASE_TABLE, "field": "really_add_order", "semantic_type": "measure",
         "comment": "净增订单数", "aliases": ["净增长订单"], "score": 0.85},
        {"table": BASE_TABLE, "field": "pure_new_order", "semantic_type": "measure",
         "comment": "纯新增订单数", "aliases": [], "score": 0.80},
        {"table": BASE_TABLE, "field": "dealership_new_order", "semantic_type": "measure",
         "comment": "经销商新增订单数", "aliases": [], "score": 0.75},
        {"table": BASE_TABLE, "field": "new_b_order", "semantic_type": "measure",
         "comment": "B类新增订单数", "aliases": [], "score": 0.70},
    ]


def _single_candidate() -> list[dict]:
    """唯一候选场景。"""
    return [_metric_candidates()[0]]


class MetricAmbiguityConfirmationFlowTest(unittest.TestCase):
    """指标口径歧义门禁与同轮锁定协议测试。"""

    def _validator(self):
        # 不依赖真实 FAISS，直接使用显式候选
        return MetricAmbiguityValidator(column_vector_store=None)

    def test_multi_candidates_block_lock_and_return_options(self):
        """“新增订单”存在多个候选且用户未选择时，必须阻止锁定并返回候选选项。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="",
            advisor_candidates=_metric_candidates(),
        )

        self.assertFalse(result.resolved)
        self.assertTrue(result.ambiguities)
        self.assertTrue(result.clarification_options)
        self.assertIn("新增订单", result.reason)

        message = _build_ambiguity_clarification(result)
        self.assertIn("new_order", message)
        self.assertIn("净增订单数", message)
        self.assertIn("请回复编号", message)

    def test_historical_case_cannot_confirm_metric(self):
        """高相似历史案例使用 new_order 时，仍不能绕过歧义门禁。"""
        validator = self._validator()
        # messages 中携带历史案例锚定文本，但用户本轮未明确选择
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="",
            messages=[{
                "role": "assistant",
                "content": "历史成功案例：查询新增订单使用字段 new_order",
            }],
            advisor_candidates=_metric_candidates(),
        )

        self.assertFalse(result.resolved)
        self.assertEqual(result.ambiguities[0]["status"], "ambiguous")
        self.assertNotEqual(result.ambiguities[0]["selected_field"], "new_order")

    def test_user_picks_full_new_order_locks_same_round(self):
        """用户回答“全量新增订单”后，本轮直接锁定 new_order。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="全量新增订单",
            advisor_candidates=_metric_candidates(),
        )

        self.assertTrue(result.resolved)
        resolution = result.resolutions[0]
        self.assertEqual(resolution["selected_field"], "new_order")
        self.assertEqual(resolution["resolution_source"], "explicit_user")

    def test_user_picks_net_new_order_locks_same_round(self):
        """用户回答“净增订单”后，本轮直接锁定 really_add_order。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="净增订单",
            advisor_candidates=_metric_candidates(),
        )

        self.assertTrue(result.resolved)
        resolution = result.resolutions[0]
        self.assertEqual(resolution["selected_field"], "really_add_order")
        self.assertEqual(resolution["resolution_source"], "explicit_user")

    def test_user_picks_b_class_new_order_locks_same_round(self):
        """用户回答“B类新增订单”后，本轮直接锁定 new_b_order。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="B类新增订单",
            advisor_candidates=_metric_candidates(),
        )

        self.assertTrue(result.resolved)
        resolution = result.resolutions[0]
        self.assertEqual(resolution["selected_field"], "new_b_order")
        self.assertEqual(resolution["resolution_source"], "explicit_user")

    def test_option_number_restores_full_semantics(self):
        """用户回复选项编号时，能结合上一轮候选恢复完整语义。"""
        validator = self._validator()
        # 候选按排序分数降序：1=new_order, 2=really_add_order
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="2",
            advisor_candidates=_metric_candidates(),
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "really_add_order")

    def test_unique_candidate_locks_without_extra_turn(self):
        """唯一指标候选不增加额外确认轮次，直接 unique_metadata 放行。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="",
            advisor_candidates=_single_candidate(),
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "new_order")
        self.assertEqual(result.resolutions[0]["resolution_source"], "unique_metadata")

    def test_forged_resolution_source_rejected(self):
        """LLM 伪造 resolution_source=explicit_user 时，程序必须拒绝。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="",
            advisor_candidates=_metric_candidates(),
        )

        self.assertFalse(result.resolved)
        verified, reason = validator.verify_submitted_resolutions(
            [{"mention": "新增订单", "field": "new_order", "source": "explicit_user"}],
            result,
        )
        self.assertFalse(verified)
        self.assertIn("新增订单", reason)

    def test_previous_resolution_kept_when_user_unchanged(self):
        """已解析记录在用户本轮未改选时保持有效，避免重复确认。"""
        validator = self._validator()
        previous = [{
            "mention": "新增订单",
            "concept_type": "metric",
            "status": "resolved",
            "selected_field": "new_order",
            "selected_table": BASE_TABLE,
            "resolution_source": "explicit_user",
            "candidates": _metric_candidates(),
        }]
        result = validator.validate(
            metric_mentions=["新增订单"],
            current_user_input="好",
            advisor_candidates=_metric_candidates(),
            previous_resolutions=previous,
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "new_order")
        self.assertEqual(result.resolutions[0]["resolution_source"], "explicit_user")

    def test_lock_query_plan_writes_concept_resolutions(self):
        """lock_query_plan 将已解决指标写入可审计 concept_resolutions。"""
        proposed_plan = {
            "tables": [BASE_TABLE],
            "measures": ["new_order"],
            "dimensions": ["company_name"],
            "time_field": "pt_dt",
            "time_range": "昨天",
            "filters": "",
            "field_sources": [f"{BASE_TABLE}.new_order"],
            "table_plans": [
                {"table": BASE_TABLE, "time_field": "pt_dt",
                 "time_range": "昨天", "filters": ""},
            ],
        }
        plan = lock_query_plan(
            proposed_plan,
            concept_resolutions={
                "新增订单": {
                    "field": "new_order",
                    "table": BASE_TABLE,
                    "source": "explicit_user",
                }
            },
        )

        self.assertEqual(plan["status"], "locked")
        self.assertEqual(
            plan["concept_resolutions"]["新增订单"]["field"],
            "new_order",
        )
        self.assertEqual(
            plan["concept_resolutions"]["新增订单"]["source"],
            "explicit_user",
        )


if __name__ == "__main__":
    unittest.main()
