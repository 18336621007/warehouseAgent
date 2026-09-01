# 维度枚举值候选发现测试（阶段0-2）：A类→company_category=A 的"指标+过滤"候选
# 覆盖：provider 发现、validator 并入、澄清展示、用户选择后 filters 落地
import unittest

from agentTest.semantic_layer.metric_matcher import match_metrics_from_query
from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.langgraph_app.services.metric_ambiguity_validator import MetricAmbiguityValidator
from agentTest.langgraph_app.services.metric_clarification_service import MetricClarificationService
from agentTest.langgraph_app.services.query_plan_service import lock_query_plan


class DimensionFilterCandidateTest(unittest.TestCase):
    """维度枚举值候选（"指标+过滤"型）端到端测试。"""

    def _validator(self):
        return MetricAmbiguityValidator(column_vector_store=None)

    def test_discover_finds_company_category_filter(self):
        """用户说 A类 + 新增订单，应发现 company_category=A 的过滤候选。"""
        provider = get_semantic_layer_provider()
        metrics = match_metrics_from_query("新增订单", limit=8)
        candidates = provider.discover_dimension_filter_candidates(
            ["A类", "新增订单"], metrics
        )
        addition = [
            c for c in candidates
            if c.get("metric_id") == "addition_order_num"
        ]
        self.assertTrue(addition, "应发现新增订单指标的过滤候选")
        target = addition[0]
        self.assertEqual(target["filter_field"], "company_category")
        self.assertEqual(target["filter_value"], "A")
        self.assertEqual(target["semantic_type"], "filter")
        self.assertIn("新增订单", target.get("metric_name", ""))

    def test_validator_includes_filter_candidates(self):
        """validator 候选池应包含过滤候选（合成 field，semantic_type=filter）。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            dimension_mentions=["A类"],
            truncate=False,
        )
        self.assertFalse(result.resolved)
        filter_candidates = []
        for ambiguity in result.ambiguities:
            filter_candidates += [
                c for c in ambiguity.get("candidates") or []
                if str(c.get("semantic_type") or "") == "filter"
            ]
        self.assertTrue(
            any(c.get("filter_field") == "company_category" for c in filter_candidates)
        )

    def test_clarification_message_shows_filter_option(self):
        """澄清文案应展示"A类（company_category=A）"口径：用户可见文案可读，
        模型注入事实仍保留合成字段标识供解析还原。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            dimension_mentions=["A类"],
            truncate=False,
        )
        message = MetricClarificationService.build_clarification_message(result)
        self.assertIn("新增订单", message)
        self.assertIn("company_category=A", message)
        # 用户可见文案不再暴露合成字段标识，改为可读过滤口径
        self.assertNotIn("__filter__addition_order_num__company_category__A", message)
        # 模型注入的候选事实仍保留合成字段标识，供选择后反查落 filters
        facts = MetricClarificationService.build_candidate_facts(result)
        self.assertIn("__filter__addition_order_num__company_category__A", facts)

    def test_user_selects_filter_locks_filters(self):
        """用户选择过滤候选后，方案应保留指标并落 filters + 维表。"""
        result = self._validator().validate(
            metric_mentions=["新增订单"],
            dimension_mentions=["A类"],
            llm_resolutions=[{
                "mention": "新增订单",
                "field": "__filter__addition_order_num__company_category__A",
                "source": "explicit_user",
            }],
            truncate=False,
        )
        self.assertTrue(result.resolved)
        plan_resolutions = self._validator().to_plan_resolutions(result)
        entry = plan_resolutions.get("新增订单") or {}
        self.assertEqual(entry.get("semantic_type"), "filter")
        self.assertEqual(entry.get("filter_field"), "company_category")

        # 模拟 advisor_graph 过滤候选落 filters 逻辑
        proposed = {
            "tables": ["dws_trip.dm_exchange_order_addition_info_hour"],
            "measures": [entry["metric_id"]],
            "dimensions": [],
            "time_field": "pt_dt",
            "time_range": "昨天",
            "filters": "",
            "field_sources": [
                "dws_trip.dm_exchange_order_addition_info_hour.addition_order_num"
            ],
            "table_plans": [],
        }
        filter_entries = [info for info in plan_resolutions.values()
                          if str(info.get("semantic_type") or "") == "filter"]
        if filter_entries:
            combined = " AND ".join(
                f"{x['filter_field']}='{x['filter_value']}'" for x in filter_entries
            )
            proposed["filters"] = combined
            for x in filter_entries:
                fm = str(x.get("filter_model") or "")
                if fm and fm not in proposed["tables"]:
                    proposed["tables"] = list(proposed["tables"]) + [fm]
                if fm and x.get("filter_field"):
                    fs = proposed.get("field_sources") or []
                    if f"{fm}.{x['filter_field']}" not in fs:
                        proposed["field_sources"] = list(fs) + [f"{fm}.{x['filter_field']}"]

        plan = lock_query_plan(proposed, concept_resolutions=plan_resolutions)
        self.assertEqual(plan["status"], "locked")
        self.assertIn("company_category='A'", plan.get("filters", ""))
        self.assertIn("addition_order_num", plan.get("measures", []))
        self.assertIn(
            "dim_trip.dim_exchange_common_company_info_day",
            plan.get("tables", []),
        )


    def test_filter_plan_end_to_end_sql_chain(self):
        """A类过滤器方案落到 confirmed_plan 后，coverage/join/fallback SQL/校验全程通过：
        过滤字段归属维表，Join 类型规范化为 INNER，fallback SQL 可直接执行。"""
        from agentTest.langgraph_app.services.query_plan_service import lock_query_plan
        from agentTest.langgraph_app.services.table_coverage_analyzer import (
            TableCoverageAnalyzer,
        )
        from agentTest.langgraph_app.services.join_planner import JoinPlanner
        from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
        from agentTest.langgraph_app.nodes.generate_sql_node import (
            _build_fallback_sql,
            _validate_sql_against_plan,
        )

        class _FakeColumnStore:
            def columns_in_table(self, table):
                return set()

        filter_entry = {
            "metric_id": "addition_order_num",
            "metric_name": "新增订单数",
            "filter_field": "company_category",
            "filter_value": "A",
            "filter_label": "A类代理商",
            "filter_model": "dim_trip.dim_exchange_common_company_info_day",
        }
        proposed = {
            "tables": ["dws_trip.dm_exchange_order_addition_info_hour",
                       "dim_trip.dim_exchange_common_company_info_day"],
            "measures": ["addition_order_num"],
            "dimensions": [],
            "time_field": "pt_dt",
            "time_range": "昨天",
            "filters": f"{filter_entry['filter_field']}='{filter_entry['filter_value']}'",
            "table_plans": [],
            "field_sources": [
                "dws_trip.dm_exchange_order_addition_info_hour.addition_order_num",
                f"{filter_entry['filter_model']}.{filter_entry['filter_field']}",
            ],
        }
        plan = lock_query_plan(proposed, concept_resolutions={})
        # 过滤字段应归属维表，而不是挂到主表 table_plan
        tp_by_table = {tp["table"]: tp for tp in plan["table_plans"]}
        self.assertIn("company_category='A'", tp_by_table[
            "dim_trip.dim_exchange_common_company_info_day"]["filters"])
        self.assertEqual(tp_by_table[
            "dws_trip.dm_exchange_order_addition_info_hour"]["filters"], "")
        # 过滤字段登记进 fields，保证字段覆盖分析确定性地纳入维表
        self.assertIn("company_category", plan.get("fields", []))

        provider = SemanticMetadataProvider()
        coverage = TableCoverageAnalyzer(_FakeColumnStore(), provider).analyze(plan)
        self.assertFalse(coverage.single_table)
        self.assertIn("dim_trip.dim_exchange_common_company_info_day",
                      coverage.needed_tables)
        join_result = JoinPlanner(provider).plan(
            coverage.needed_tables, coverage.field_sources
        )
        self.assertTrue(join_result.success)
        plan["joins"] = join_result.join_edges
        plan["field_sources"] = join_result.field_sources
        plan["tables"] = coverage.needed_tables
        plan["table"] = coverage.needed_tables[0]

        sql = _build_fallback_sql(plan)
        self.assertIn("INNER JOIN", sql.upper())
        self.assertNotIn("join JOIN", sql.lower())
        # 过滤条件必须挂在维表别名上，而不是主表别名
        self.assertIn("b.company_category='a'", sql.lower().replace(" ", ""))
        issues = _validate_sql_against_plan(sql, plan)
        self.assertEqual(issues, [], "fallback SQL 应通过方案校验")


if __name__ == "__main__":
    unittest.main()
