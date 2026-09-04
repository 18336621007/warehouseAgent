# 语义层 grep 两阶段流程集成测试：mock LLM + 向量库，验证 planner 分档路由
# 覆盖：grep 命中注入、semantic_metrics 分档、强命中短路跳过 FAISS、Advisor 候选传递
import unittest
from unittest import mock

from agentTest.langgraph_app.prompts.planner_prompt import (
    PlannerOutput,
    SemanticKeywordsOutput,
    SemanticMetricHit,
)


class _FakeVectorStore:
    """极简向量库桩：返回空检索结果。"""

    def similarity_search_with_score(self, question, k, **kwargs):
        return []


class _FakeReranker:
    def retrieve(self, question, top_k):
        return []


class _FakeStructuredLLM:
    """按结构化模型类型返回预设输出（keyword 小调用 / 完整 PlannerOutput）。"""

    def __init__(self, keyword_list, planner_kwargs):
        self._keyword_list = keyword_list
        self._planner_kwargs = planner_kwargs

    def invoke(self, prompt_value):
        return self  # 简化：invoke 直接返回

    def with_structured_output(self, model):
        if model is SemanticKeywordsOutput:
            return _FakeStructuredCallable(
                SemanticKeywordsOutput(semantic_keywords=self._keyword_list)
            )
        return _FakeStructuredCallable(PlannerOutput(**self._planner_kwargs))


class _FakeStructuredCallable:
    def __init__(self, value):
        self._value = value

    def invoke(self, prompt_value):
        return self._value


def _build_runtime():
    from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
    return {
        "table_vector_store": _FakeVectorStore(),
        "column_vector_store": _FakeVectorStore(),
        "bm25_retriever": None,
        "example_vector_store": None,
        "semantic_metadata_provider": SemanticMetadataProvider(),
    }


def _state(user_input, messages=None):
    return {
        "current_user_input": user_input,
        "messages": messages or [],
        "confirmed_plan": {},
        "analysis_spec": {},
    }


def _planner_kwargs(**overrides):
    base = {
        "effective_query": "查询昨天从山东瀛能公司调出的调出明细",
        "route": "advisor",
        "tables": ["ads_trip.ads_gundam_device_transfer_detail_hour"],
        "fields": ["origin_company_name", "transfer_no"],
        "completeness": "full",
        "complex": False,
        "metric_mentions": ["调出明细"],
        "dimension_mentions": ["山东瀛能"],
        "analysis_type": "detail",
        "reason": "语义层命中调货明细",
        "follow_up_mode": "new_query",
        "semantic_keywords": ["调出", "明细"],
        "semantic_metrics": [],
    }
    base.update(overrides)
    return base


class PlannerSemanticGrepFlowTest(unittest.TestCase):
    """Planner 两阶段语义层流程测试。"""

    def _run_planner(self, keyword_list, planner_kwargs):
        from agentTest.langgraph_app.nodes import planner_node

        fake_llm = _FakeStructuredLLM(keyword_list, planner_kwargs)
        with mock.patch.object(planner_node, "ChatOpenAI", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state("查询昨天从山东瀛能公司调出的调出明细"))

    def test_strong_grep_shortcut_skips_faiss(self):
        """调出明细：grep 强命中 device_transfer_detail，LLM 唯一强命中 → semantic unique 短路。"""
        planner_kwargs = _planner_kwargs(
            semantic_metrics=[
                SemanticMetricHit(
                    id="device_transfer_detail",
                    confidence=0.95,
                    mention="调出明细",
                )
            ]
        )
        result = self._run_planner(["调出", "明细"], planner_kwargs)
        entities = result["planner_entities"]
        self.assertTrue(entities["semantic_metrics"])
        self.assertEqual(
            entities["semantic_metrics"][0]["id"],
            "device_transfer_detail",
        )
        # 语义候选应透传给 Advisor
        self.assertTrue(
            any(
                c.get("id") == "device_transfer_detail"
                for c in entities["semantic_candidates"]
            )
        )
        # table_candidates 来自语义层推荐
        self.assertTrue(
            any(
                t.get("table") == "ads_trip.ads_gundam_device_transfer_detail_hour"
                for t in entities["table_candidates"]
            )
        )

    def test_candidate_tier_routes_to_advisor(self):
        """0.55~0.9 候选反问：不短路，保留语义候选供 Advisor 澄清。"""
        planner_kwargs = _planner_kwargs(
            completeness="partial",
            semantic_metrics=[
                SemanticMetricHit(
                    id="device_transfer_detail",
                    confidence=0.8,
                    mention="调出",
                ),
                SemanticMetricHit(
                    id="device_return_detail",
                    confidence=0.7,
                    mention="调出",
                ),
            ],
        )
        result = self._run_planner(["调出", "明细"], planner_kwargs)
        self.assertEqual(result["route"], "advisor")
        entities = result["planner_entities"]
        # 两个候选都传给 Advisor，供澄清
        ids = {m["id"] for m in entities["semantic_metrics"]}
        self.assertIn("device_transfer_detail", ids)
        self.assertIn("device_return_detail", ids)

    def test_no_semantic_grep_goes_rag(self):
        """无 grep 命中：走 RAG（FAISS），semantic_metrics 为空。"""
        planner_kwargs = _planner_kwargs(
            semantic_keywords=["排产"],
            semantic_metrics=[],
        )
        result = self._run_planner(["排产电人比"], planner_kwargs)
        entities = result["planner_entities"]
        self.assertEqual(entities["semantic_metrics"], [])
        # 仍走 advisor（正常解析链路）
        self.assertIn(result["route"], ("advisor", "seeker"))

    def test_seeker_route_builds_plan_from_semantic(self):
        """Planner 判定 seeker 且语义层唯一强命中时，确定性构建 confirmed_plan。"""
        planner_kwargs = _planner_kwargs(
            route="seeker",
            effective_query="查询昨天的新增订单数",
            dimension_mentions=[],
            semantic_metrics=[
                SemanticMetricHit(
                    id="addition_order_num",
                    confidence=0.95,
                    mention="新增订单",
                )
            ],
        )
        result = self._run_planner(["新增订单"], planner_kwargs)
        self.assertEqual(result["route"], "seeker")
        plan = result.get("confirmed_plan") or {}
        self.assertEqual(plan.get("status"), "locked")
        self.assertIn("ads_trip.ads_region_rent_order_analysis_hour", plan.get("tables", []))
        self.assertIn("new_rent_counts", plan.get("measures", []))

    def test_seeker_route_falls_back_to_advisor_on_build_failure(self):
        """Planner 判定 seeker 但语义层无法确定性构建（复合指标）时降级 advisor。"""
        planner_kwargs = _planner_kwargs(
            route="seeker",
            effective_query="查询昨天的续租率",
            dimension_mentions=[],
            semantic_metrics=[
                SemanticMetricHit(
                    id="renewal_rate",
                    confidence=0.95,
                    mention="续租率",
                )
            ],
        )
        result = self._run_planner(["续租率"], planner_kwargs)
        self.assertEqual(result["route"], "advisor")
        self.assertIsNone(result.get("confirmed_plan"))


if __name__ == "__main__":
    unittest.main()
