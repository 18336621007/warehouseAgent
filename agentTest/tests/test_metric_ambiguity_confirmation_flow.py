# 第12课：AnalysisSpec 与指标口径歧义门禁测试。
# 覆盖：多候选拦截、历史案例不能确认口径、模型判断选择+程序白名单校验后同轮锁定、唯一候选直锁、候选外字段拒绝。
import unittest

from agentTest.langgraph_app.services.metric_ambiguity_validator import MetricAmbiguityValidator
from agentTest.langgraph_app.services.metric_clarification_service import MetricClarificationService
from agentTest.langgraph_app.services.query_plan_service import lock_query_plan

BASE_TABLE = "ads_trip.ads_exchange_platform_operations_report_day"


def _metric_candidates() -> list[dict]:
    """构造与真实日志一致的“新增订单”多候选字段列表（字段文档格式，含原始备注）。"""
    return [
        {"table": BASE_TABLE, "field": "new_order", "semantic_type": "measure",
         "comment": "字段: ads_trip.xxx.new_order\n类型: 【度量】\n别名: 新增订单\n原始备注: 全量新增订单数",
         "aliases": ["新增订单"], "score": 0.90},
        {"table": BASE_TABLE, "field": "really_add_order", "semantic_type": "measure",
         "comment": "字段: ads_trip.xxx.really_add_order\n类型: 【度量】\n别名: 净增长订单\n原始备注: 净增订单数",
         "aliases": ["净增长订单"], "score": 0.85},
        {"table": BASE_TABLE, "field": "pure_new_order", "semantic_type": "measure",
         "comment": "字段: ads_trip.xxx.pure_new_order\n类型: 【度量】\n原始备注: 纯新增订单数",
         "aliases": [], "score": 0.80},
        {"table": BASE_TABLE, "field": "dealership_new_order", "semantic_type": "measure",
         "comment": "字段: ads_trip.xxx.dealership_new_order\n类型: 【度量】\n原始备注: 经销商新增订单数",
         "aliases": [], "score": 0.75},
        {"table": BASE_TABLE, "field": "new_b_order", "semantic_type": "measure",
         "comment": "字段: ads_trip.xxx.new_b_order\n类型: 【度量】\n原始备注: B类新增订单数",
         "aliases": [], "score": 0.70},
    ]


def _single_candidate() -> list[dict]:
    """唯一候选场景。"""
    return [_metric_candidates()[0]]


def _previous_new_order() -> list[dict]:
    """上轮已确认 new_order 的解析记录。"""
    return [{
        "mention": "新增订单",
        "concept_type": "metric",
        "status": "resolved",
        "selected_field": "new_order",
        "selected_table": BASE_TABLE,
        "resolution_source": "explicit_user",
        "candidates": _metric_candidates(),
    }]


class MetricAmbiguityConfirmationFlowTest(unittest.TestCase):
    """指标口径歧义门禁与同轮锁定协议测试。"""

    def _validator(self):
        # 不依赖真实 FAISS，直接使用显式候选
        return MetricAmbiguityValidator(column_vector_store=None)

    def test_multi_candidates_block_lock_and_return_options(self):
        """“新增订单”存在多个候选且 LLM 未提交解析时，必须阻止锁定并返回候选选项。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )

        self.assertFalse(result.resolved)
        self.assertTrue(result.ambiguities)
        self.assertTrue(result.clarification_options)
        self.assertIn("新增订单", result.reason)

        message = MetricClarificationService.build_clarification_message(result)
        self.assertIn("new_order", message)
        self.assertIn("净增订单数", message)
        self.assertIn("请回复编号", message)

    def test_historical_case_cannot_confirm_metric(self):
        """高相似历史案例使用 new_order 时，仍不能绕过歧义门禁。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )

        self.assertFalse(result.resolved)
        self.assertEqual(result.ambiguities[0]["status"], "ambiguous")
        self.assertNotEqual(result.ambiguities[0]["selected_field"], "new_order")

    def test_user_picks_full_new_order_locks_same_round(self):
        """用户回答“全量新增订单”，LLM 提交 new_order 后本轮直接锁定。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "new_order",
                "source": "explicit_user",
            }],
        )

        self.assertTrue(result.resolved)
        resolution = result.resolutions[0]
        self.assertEqual(resolution["selected_field"], "new_order")
        self.assertEqual(resolution["resolution_source"], "llm_submitted")

    def test_user_picks_net_new_order_locks_same_round(self):
        """用户回答“净增订单”，LLM 提交 really_add_order 后本轮直接锁定。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "really_add_order",
                "source": "explicit_user",
            }],
        )

        self.assertTrue(result.resolved)
        resolution = result.resolutions[0]
        self.assertEqual(resolution["selected_field"], "really_add_order")
        self.assertEqual(resolution["resolution_source"], "llm_submitted")

    def test_user_picks_b_class_new_order_locks_same_round(self):
        """用户回答“B类新增订单”，LLM 提交 new_b_order 后本轮直接锁定。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "new_b_order",
                "source": "explicit_user",
            }],
        )

        self.assertTrue(result.resolved)
        resolution = result.resolutions[0]
        self.assertEqual(resolution["selected_field"], "new_b_order")
        self.assertEqual(resolution["resolution_source"], "llm_submitted")

    def test_option_number_restores_full_semantics(self):
        """用户回复编号 2，LLM 结合上轮候选解析为 really_add_order 并提交，程序信任。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "really_add_order",
                "source": "explicit_user",
            }],
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "really_add_order")

    def test_unique_candidate_locks_without_extra_turn(self):
        """唯一指标候选不增加额外确认轮次，直接 unique_metadata 放行。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_single_candidate(),
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "new_order")
        self.assertEqual(result.resolutions[0]["resolution_source"], "unique_metadata")

    def test_llm_submitted_field_outside_candidates_rejected(self):
        """LLM 提交候选外字段时不被信任，仍按歧义处理并追问。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "made_up_order",
                "source": "explicit_user",
            }],
        )

        self.assertFalse(result.resolved)
        self.assertEqual(result.ambiguities[0]["status"], "ambiguous")
        self.assertNotEqual(result.ambiguities[0]["selected_field"], "made_up_order")

    def test_llm_resubmits_same_field_keeps_previous(self):
        """LLM 重新提交与上轮一致的字段时，保留上轮解析证据不重复确认。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            previous_resolutions=_previous_new_order(),
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "new_order",
                "source": "explicit_user",
            }],
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "new_order")
        self.assertEqual(result.resolutions[0]["resolution_source"], "explicit_user")

    def test_llm_submits_new_valid_field_overrides_previous(self):
        """用户改选后 LLM 提交新字段，程序以 LLM 解析为准覆盖上轮记录。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            previous_resolutions=_previous_new_order(),
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "really_add_order",
                "source": "explicit_user",
            }],
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "really_add_order")
        self.assertEqual(result.resolutions[0]["resolution_source"], "llm_submitted")

    def test_build_meaning_prefers_original_comment(self):
        """口径展示优先原始备注，无原始备注时回退首个别名。"""
        candidate = {
            "table": "ads_trip.ads_exchange_platform_operations_report_day",
            "field": "this_day_renting_order",
            "comment": (
                "字段: ads_trip.ads_exchange_platform_operations_report_day.this_day_renting_order\n"
                "类型: 【度量】\n"
                "别名: 当日租赁中订单数、日租进行中订单量\n"
                "原始备注: 日租租赁中订单数"
            ),
            "aliases": ["当日租赁中订单数", "日租进行中订单量"],
        }
        # 原始备注存在时，不能用别名“当日租赁中订单数”顶替
        self.assertEqual(
            MetricAmbiguityValidator._build_meaning(candidate),
            "日租租赁中订单数",
        )
        # 无原始备注时回退首个别名
        candidate["comment"] = "字段: xxx.yyy\n类型: 【度量】\n别名: 当日租赁中订单数"
        candidate["aliases"] = ["当日租赁中订单数"]
        self.assertEqual(
            MetricAmbiguityValidator._build_meaning(candidate),
            "当日租赁中订单数",
        )

    def test_previous_resolution_kept_when_user_unchanged(self):
        """已解析记录在用户本轮未改选且 LLM 未提交时保持有效，避免重复确认。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            previous_resolutions=_previous_new_order(),
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "new_order")
        self.assertEqual(result.resolutions[0]["resolution_source"], "explicit_user")

    def test_unresolved_result_persists_fixed_pending_options(self):
        """首轮多候选必须写入待确认状态，下一轮编号不能依赖重新召回顺序。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )

        updated_spec = MetricClarificationService.update_analysis_spec(
            {"metric_mentions": ["新增订单"]},
            result,
            "request-1",
        )

        pending = updated_spec["pending_clarifications"][0]
        self.assertEqual(pending["clarification_id"], "request-1")
        self.assertEqual(pending["mention"], "新增订单")
        self.assertEqual(pending["status"], "open")
        self.assertEqual(pending["options"][1]["field"], "really_add_order")

    def test_unresolved_result_preserves_existing_open_pending(self):
        """用户未选择时 update_analysis_spec 复用原 open pending，编号不随后续召回变化。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )
        current_spec = MetricClarificationService.update_analysis_spec(
            {"metric_mentions": ["新增订单"]},
            result,
            "request-1",
        )
        first_pending = current_spec["pending_clarifications"][0]

        # 第二轮用户只问解释，再次触发未解析 → 复用原 pending（clarification_id/options 不变）
        again = MetricClarificationService.update_analysis_spec(
            current_spec,
            result,
            "request-2",
        )
        second_pending = again["pending_clarifications"][0]

        self.assertEqual(second_pending["clarification_id"], first_pending["clarification_id"])
        self.assertEqual(
            [option["field"] for option in second_pending["options"]],
            [option["field"] for option in first_pending["options"]],
        )

    def test_validate_user_selection_accepts_model_choice(self):
        """模型从历史对话判断用户选择了某候选，程序白名单校验通过后生成 explicit_user。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )
        pending = MetricClarificationService.build_pending_clarification(result)

        resolution = MetricClarificationService.validate_user_selection(
            {
                "selected": True,
                "clarification_id": pending["clarification_id"],
                "field": "really_add_order",
                "reasoning": "用户回复“2”",
            },
            pending,
        )

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution["selected_field"], "really_add_order")
        self.assertEqual(resolution["resolution_source"], "explicit_user")

    def test_validate_user_selection_rejects_outside_field(self):
        """模型误判候选外字段时程序拒绝，宁可不选也不猜测。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )
        pending = MetricClarificationService.build_pending_clarification(result)

        resolution = MetricClarificationService.validate_user_selection(
            {
                "selected": True,
                "clarification_id": pending["clarification_id"],
                "field": "made_up_order",
                "reasoning": "用户提到某字段",
            },
            pending,
        )

        self.assertIsNone(resolution)

    def test_validate_user_selection_requires_explicit_selection(self):
        """用户只是询问解释时 selected=false，不能视为已选择。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )
        pending = MetricClarificationService.build_pending_clarification(result)

        resolution = MetricClarificationService.validate_user_selection(
            {"selected": False, "clarification_id": "", "field": "", "reasoning": ""},
            pending,
        )

        self.assertIsNone(resolution)

    def test_validate_user_selection_rejects_wrong_clarification_id(self):
        """clarification_id 对不上当前 open pending 时拒绝。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
        )
        pending = MetricClarificationService.build_pending_clarification(result)

        resolution = MetricClarificationService.validate_user_selection(
            {
                "selected": True,
                "clarification_id": "stale-id",
                "field": "really_add_order",
                "reasoning": "选择 2",
            },
            pending,
        )

        self.assertIsNone(resolution)

    def test_previous_selection_survives_candidate_reordering(self):
        """重新召回缺少已选字段时，仍认可上轮真实候选中的用户选择。"""
        validator = self._validator()
        result = validator.validate(
            metric_mentions=["新增订单"],
            advisor_candidates=[_metric_candidates()[1]],
            previous_resolutions=_previous_new_order(),
            llm_resolutions=[],
        )

        self.assertTrue(result.resolved)
        self.assertEqual(result.resolutions[0]["selected_field"], "new_order")
        self.assertEqual(result.resolutions[0]["resolution_source"], "explicit_user")

    def test_resolved_metric_is_exposed_to_advisor_context(self):
        """用户已选字段必须进入 Advisor 上下文，避免模型重新猜测口径。"""
        context = MetricClarificationService.build_resolution_context({
            "metric_resolutions": _previous_new_order(),
        })

        self.assertIn("新增订单", context)
        self.assertIn(f"{BASE_TABLE}.new_order", context)
        self.assertIn("不得再次追问", context)

    def test_resolved_result_clears_pending_state(self):
        """用户选择已解析后自动清理 pending，避免下一轮继续重复确认。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            advisor_candidates=_metric_candidates(),
            previous_resolutions=_previous_new_order(),
            llm_resolutions=[],
        )
        current_spec = {
            "metric_mentions": ["新增订单"],
            "pending_clarifications": [
                {"clarification_id": "request-1", "mention": "新增订单",
                 "status": "open", "options": []},
            ],
        }

        updated_spec = MetricClarificationService.update_analysis_spec(current_spec, result)

        self.assertEqual(updated_spec["pending_clarifications"], [])
        self.assertEqual(updated_spec["metric_resolutions"][0]["status"], "resolved")

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
                    "source": "llm_submitted",
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
            "llm_submitted",
        )


if __name__ == "__main__":
    unittest.main()
