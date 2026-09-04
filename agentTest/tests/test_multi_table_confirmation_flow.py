# 多表确认协议与复合Join键回归测试。
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agentTest.db.hive_guardrails import ALLOW_AI_INFERRED_JOIN
from agentTest.db.hive_guardrails import REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES
from agentTest.langgraph_app.graphs.advisor_graph import _normalize_clarification_message
from agentTest.langgraph_app.nodes.generate_sql_node import _build_fallback_sql
from agentTest.langgraph_app.nodes.generate_sql_node import _validate_sql_against_plan
from agentTest.langgraph_app.nodes.generate_sql_node import _repair_missing_table_filters
from agentTest.langgraph_app.nodes.validate_sql_node import _validate_multi_table
from agentTest.langgraph_app.services.join_planner import JoinPlanner
from agentTest.langgraph_app.services.sql_table_filter_validator import validate_table_plan_filters
from agentTest.langgraph_app.services.query_plan_service import lock_query_plan


def _build_plan() -> dict:
    """构造与日志场景一致的两表查询方案。"""
    return {
        "table": "ads_trip.ads_exchange_platform_operations_report_day",
        "tables": [
            "ads_trip.ads_exchange_platform_operations_report_day",
            "dim_trip.dim_company_snapshot_day",
        ],
        "measures": ["new_order"],
        "dimensions": ["company_name", "company_business_type", "sales_manager"],
        "time_field": "pt_dt",
        "time_range": "昨天",
        "filters": "",
        "having": "",
        "order_by": [{"field": "new_order", "direction": "DESC"}],
        "result_limit": 1,
        "field_sources": {
            "new_order": "ads_trip.ads_exchange_platform_operations_report_day",
            "company_name": "ads_trip.ads_exchange_platform_operations_report_day",
            "company_business_type": "dim_trip.dim_company_snapshot_day",
            "sales_manager": "dim_trip.dim_company_snapshot_day",
            "pt_dt": "ads_trip.ads_exchange_platform_operations_report_day",
        },
        "table_plans": [
            {
                "table": "ads_trip.ads_exchange_platform_operations_report_day",
                "time_field": "pt_dt",
                "time_range": "昨天",
                "filters": "",
            },
            {
                "table": "dim_trip.dim_company_snapshot_day",
                "time_field": "pt_dt",
                "time_range": "昨天",
                "filters": "",
            },
        ],
        "joins": [
            {
                "left_table": "ads_trip.ads_exchange_platform_operations_report_day",
                "right_table": "dim_trip.dim_company_snapshot_day",
                "left_key": ["pt_platform", "company_id"],
                "right_key": ["pt_platform", "company_id"],
                "join_type": "LEFT",
                "cardinality": "many_to_one",
            }
        ],
    }


class MultiTableConfirmationFlowTest(unittest.TestCase):
    """覆盖单次确认和多表Join安全规则。"""


    def test_fallback_sql_uses_composite_keys_and_keeps_dimension_filter_in_on(self):
        """复合键必须完整生成，维表分区条件不能让LEFT JOIN退化。"""
        sql = _build_fallback_sql(_build_plan())
        on_clause, where_clause = sql.split("WHERE", 1)

        self.assertIn("a.pt_platform = b.pt_platform", on_clause)
        self.assertIn("a.company_id = b.company_id", on_clause)
        self.assertIn("b.pt_dt = regexp_replace", on_clause)
        self.assertIn("a.pt_dt = regexp_replace", where_clause)
        self.assertNotIn("b.pt_dt = regexp_replace", where_clause)

    def test_plan_consistency_accepts_qualified_aggregate_field(self):
        """方案一致性校验必须识别带表别名的聚合字段。"""
        plan = _build_plan()
        sql = _build_fallback_sql(plan)

        self.assertEqual(_validate_sql_against_plan(sql, plan), [])

    def test_lock_query_plan_completes_missing_table_filter_plan(self):
        """Advisor漏交某张表的table_plan时，锁定服务必须自动补齐。"""
        proposed_plan = {
            "tables": [
                "ads_trip.ads_exchange_platform_operations_report_day",
                "dim_trip.dim_company_snapshot_day",
            ],
            "measures": ["new_order"],
            "dimensions": ["company_business_type"],
            "time_field": "pt_dt",
            "time_range": "昨天",
            "filters": "platform_type = '换电'",
            "field_sources": [
                "ads_trip.ads_exchange_platform_operations_report_day.new_order",
                "dim_trip.dim_company_snapshot_day.company_business_type",
            ],
            "table_plans": [
                {
                    "table": "ads_trip.ads_exchange_platform_operations_report_day",
                    "time_field": "pt_dt",
                    "time_range": "昨天",
                    "filters": "",
                }
            ],
        }

        locked_plan = lock_query_plan(proposed_plan)
        plan_by_table = {
            table_plan["table"]: table_plan
            for table_plan in locked_plan["table_plans"]
        }
        self.assertEqual(len(plan_by_table), 2)
        self.assertEqual(
            plan_by_table["dim_trip.dim_company_snapshot_day"]["time_field"],
            "pt_dt",
        )
        self.assertEqual(
            plan_by_table["dim_trip.dim_company_snapshot_day"]["time_range"],
            "昨天",
        )
        self.assertEqual(
            plan_by_table["ads_trip.ads_exchange_platform_operations_report_day"]["filters"],
            "platform_type = '换电'",
        )
        self.assertEqual(
            plan_by_table["dim_trip.dim_company_snapshot_day"]["filters"],
            "",
        )

    def test_missing_dimension_time_filter_triggers_deterministic_repair(self):
        """日志同款SQL缺少维表pt_dt时必须自动重建为安全SQL。"""
        plan = _build_plan()
        sql = (
            "SELECT b.true_name, SUM(a.new_order) AS new_order "
            "FROM ads_trip.ads_exchange_platform_operations_report_day a "
            "LEFT JOIN dim_trip.dim_company_snapshot_day b "
            "ON a.pt_platform = b.pt_platform AND a.company_id = b.company_id "
            "WHERE a.pt_dt = regexp_replace(date_sub(current_date(), 1), '-', '') "
            "GROUP BY b.true_name LIMIT 1"
        )

        issues = validate_table_plan_filters(sql, plan["tables"], plan["table_plans"])
        self.assertTrue(any("dim_trip.dim_company_snapshot_day" in issue for issue in issues))
        self.assertTrue(any("b.pt_dt" in issue for issue in issues))
        consistency_issues = _validate_sql_against_plan(sql, plan)
        self.assertTrue(any("b.pt_dt" in issue for issue in consistency_issues))

        repaired_sql, repair_reasons = _repair_missing_table_filters(sql, plan)
        self.assertTrue(repair_reasons)
        self.assertNotEqual(repaired_sql, sql)
        self.assertIn("b.pt_dt = regexp_replace", repaired_sql)
        self.assertEqual(
            validate_table_plan_filters(
                repaired_sql,
                plan["tables"],
                plan["table_plans"],
            ),
            [],
        )

    def test_partition_key_join_does_not_replace_dimension_filter(self):
        """维表pt_dt仅与事实表pt_dt关联时，仍必须补充独立日期过滤。"""
        plan = _build_plan()
        sql = (
            "SELECT b.true_name, SUM(a.new_order) AS new_order "
            "FROM ads_trip.ads_exchange_platform_operations_report_day a "
            "LEFT JOIN dim_trip.dim_company_snapshot_day b "
            "ON a.pt_platform = b.pt_platform AND a.company_id = b.company_id "
            "AND a.pt_dt = b.pt_dt "
            "WHERE a.pt_dt = regexp_replace(date_sub(current_date(), 1), '-', '') "
            "GROUP BY b.true_name LIMIT 1"
        )

        issues = validate_table_plan_filters(sql, plan["tables"], plan["table_plans"])
        self.assertTrue(any("b.pt_dt" in issue for issue in issues))

    def test_multi_table_validator_requires_all_composite_keys(self):
        """多表校验必须拒绝缺少任一复合Join键的SQL。"""
        plan = _build_plan()
        valid_sql = _build_fallback_sql(plan)
        invalid_sql = valid_sql.replace(
            "a.pt_platform = b.pt_platform AND ",
            "",
        )

        self.assertEqual(_validate_multi_table(valid_sql, plan), [])
        issues = _validate_multi_table(invalid_sql, plan)
        self.assertTrue(any("pt_platform=pt_platform" in issue for issue in issues))

    def test_global_required_filter_configuration_drives_every_table(self):
        """全局配置字段必须在每张参与表上形成真实过滤。"""
        plan = _build_plan()
        sql = _build_fallback_sql(plan)
        self.assertEqual(REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES, ["pt_dt"])

        with patch(
            "agentTest.langgraph_app.services.sql_table_filter_validator.REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES",
            ["pt_platform"],
        ):
            issues = validate_table_plan_filters(
                sql,
                plan["tables"],
                plan["table_plans"],
            )
        self.assertTrue(any("a.pt_platform" in issue for issue in issues))
        self.assertTrue(any("b.pt_platform" in issue for issue in issues))

    def test_join_inference_remains_controlled_by_enabled_switch(self):
        """开关开启时允许推测，关闭时仍安全拒绝。"""
        class EmptySemanticProvider:
            # 测试Provider不提供任何人工关系。
            def get_relations_for_table(self, table_identifier):
                return []

            def find_safe_join_path(self, models):
                return []

            def get_table_grain(self, table_identifier):
                return ""

        planner = JoinPlanner(EmptySemanticProvider())
        tables = ["db.fact", "db.dim"]
        sources = {"metric": "db.fact", "name": "db.dim"}

        with patch("agentTest.db.hive_guardrails.ALLOW_AI_INFERRED_JOIN", True):
            inferred = planner.plan(tables, sources)
        self.assertTrue(inferred.success)
        self.assertTrue(inferred.needs_ai_inference)

        with patch("agentTest.db.hive_guardrails.ALLOW_AI_INFERRED_JOIN", False):
            rejected = planner.plan(tables, sources)
        self.assertFalse(rejected.success)
        self.assertFalse(rejected.needs_ai_inference)
        self.assertTrue(ALLOW_AI_INFERRED_JOIN)

    def test_semantic_relation_uses_composite_business_key(self):
        """语义层 join_contracts 中事实表与经销商维表按复合键关联。"""
        from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
        provider = SemanticMetadataProvider()
        contracts = provider.get_all_enabled_relations()
        target = next(
            (c for c in contracts
             if c.get("left_model") == "dws_trip.dm_exchange_order_addition_info_hour"
             and c.get("right_model") == "dim_trip.dim_exchange_common_company_info_day"),
            None,
        )
        self.assertIsNotNone(target)
        # 复合键（pt_platform / pt_dt / company_id 等，至少 3 个）
        on_list = target.get("on") or []
        self.assertTrue(len(on_list) >= 3)


if __name__ == "__main__":
    unittest.main()