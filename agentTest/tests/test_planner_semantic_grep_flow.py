# 语义层 grep 两阶段流程集成测试：mock LLM + 向量库，验证 planner 分档路由
# 覆盖：grep 命中注入、semantic_metrics 分档、强命中短路跳过 FAISS、Advisor 候选传递
import unittest
from unittest import mock

from langchain_core.messages import AIMessage
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


class _FakeTool:
    """轻量工具桩：可调用、返回固定结果，模拟 Planner registry 工具。"""

    def __init__(self, name, result="工具结果"):
        self.name = name
        self.result = result

    def invoke(self, args):
        return f"{self.name} -> {self.result}"


class _FakeReactLLM:
    """ReAct 循环桩：按预设序列返回 tool_calls，序列耗尽后返回无 tool_calls。"""

    def __init__(self, tool_calls_list):
        self._tool_calls_list = list(tool_calls_list)
        self._step = 0

    def invoke(self, messages):
        if self._step < len(self._tool_calls_list):
            tc = self._tool_calls_list[self._step]
            self._step += 1
            return AIMessage(content="", tool_calls=[tc])
        return AIMessage(content="信息已充分，直接输出判定")


class _FakeStructuredLLM:
    """按结构化模型类型返回预设输出（keyword 小调用 / 完整 PlannerOutput）。"""

    def __init__(self, keyword_list, planner_kwargs, react_tool_calls=None, seen_messages=None):
        self._keyword_list = keyword_list
        self._planner_kwargs = planner_kwargs
        self._react_tool_calls = react_tool_calls or []
        self._seen_messages = seen_messages  # 记录传给结构化 LLM 的 messages，供断言工具回填

    def invoke(self, prompt_value):
        return self  # 简化：invoke 直接返回

    def bind_tools(self, tools):
        return _FakeReactLLM(self._react_tool_calls)

    def with_structured_output(self, model):
        if model is SemanticKeywordsOutput:
            return _FakeStructuredCallable(
                SemanticKeywordsOutput(semantic_keywords=self._keyword_list)
            )
        return _FakeStructuredCallable(PlannerOutput(**self._planner_kwargs), self._seen_messages)


class _FakeStructuredCallable:
    def __init__(self, value, seen_messages=None):
        self._value = value
        self._seen_messages = seen_messages

    def invoke(self, prompt_value):
        # 工具链场景：记录最终结构化输入，供测试断言工具结果已回填
        if self._seen_messages is not None:
            self._seen_messages.append(prompt_value)
        return self._value


def _build_runtime():
    from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
    from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec
    # M2：Planner 从统一注册表取 planner 组工具（本测试用桩工具）
    registry = ToolRegistry()
    for name in ("search_databases", "search_tables", "search_columns", "query_stored_result", "search_semantic"):
        registry.register(ToolSpec(name=name, description="stub", tool=_FakeTool(name), groups=("planner",)))
    return {
        "table_vector_store": _FakeVectorStore(),
        "column_vector_store": _FakeVectorStore(),
        "bm25_retriever": None,
        "example_vector_store": None,
        "tool_registry": registry,
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
        "route": "execute",
        "tables": ["ads_trip.ads_gundam_device_transfer_detail_hour"],
        "fields": ["origin_company_name", "transfer_no"],
        "completeness": "full",
        "complex": False,
        "metric_mentions": ["调出明细"],
        "dimension_mentions": ["山东瀛能"],
        "analysis_type": "detail",
        # 明细查询必须由 filters 明确业务时间字段（无分区明细表禁止回退 pt_dt）
        "filters": "pt_dt 昨天",
        "reason": "语义层命中调货明细",
        "semantic_keywords": ["调出", "明细"],
        "semantic_metrics": [],
    }
    base.update(overrides)
    return base


class PlannerSemanticGrepFlowTest(unittest.TestCase):
    """Planner 两阶段语义层流程测试。"""

    def _run_planner(self, keyword_list, planner_kwargs, react_tool_calls=None, seen_messages=None):
        from agentTest.langgraph_app.nodes import planner_node

        fake_llm = _FakeStructuredLLM(keyword_list, planner_kwargs, react_tool_calls, seen_messages)
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
        # 语义候选保留在 planner_entities（供日志/trace 与后续轮次参考）
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

    def test_candidate_tier_routes_to_respond(self):
        """0.55~0.9 候选反问：Planner 直接 respond 澄清（不再降级 Advisor）。"""
        planner_kwargs = _planner_kwargs(
            route="respond",
            completeness="partial",
            respond_text="您说的“调出”可能对应多个口径：1) 调货明细；2) 返厂明细。请确认是哪一个？",
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
        self.assertEqual(result["route"], "respond")
        entities = result["planner_entities"]
        # 两个候选都保留在 planner_entities，供日志/trace 与后续轮次参考
        ids = {m["id"] for m in entities["semantic_metrics"]}
        self.assertIn("device_transfer_detail", ids)
        self.assertIn("device_return_detail", ids)

    def test_no_semantic_grep_goes_execute(self):
        """无 grep 命中：semantic_metrics 为空，Planner 用输出构造最小方案直通执行链。"""
        planner_kwargs = _planner_kwargs(
            route="execute",
            semantic_keywords=["排产"],
            semantic_metrics=[],
        )
        result = self._run_planner(["排产电人比"], planner_kwargs)
        entities = result["planner_entities"]
        self.assertEqual(entities["semantic_metrics"], [])
        # execute：方案由 Planner 输出构造（最小方案），route 收敛为 execute
        self.assertEqual(result["route"], "execute")
        self.assertIsNotNone(result.get("confirmed_plan"))

    def test_execute_route_builds_plan_from_semantic(self):
        """Planner 判定 execute 且语义层唯一强命中时，确定性构建 confirmed_plan。"""
        planner_kwargs = _planner_kwargs(
            route="execute",
            effective_query="查询昨天的新增订单数",
            dimension_mentions=[],
            filters="pt_dt 昨天",
            semantic_metrics=[
                SemanticMetricHit(
                    id="addition_order_num",
                    confidence=0.95,
                    mention="新增订单",
                )
            ],
        )
        result = self._run_planner(
            ["新增订单"],
            planner_kwargs,
            react_tool_calls=[{
                "name": "search_semantic",
                "args": {"question": "新增订单"},
                "id": "call_semantic_1",
            }],
        )
        self.assertEqual(result["route"], "execute")
        plan = result.get("confirmed_plan") or {}
        self.assertEqual(plan.get("status"), "confirmed")
        self.assertIn("ads_trip.ads_region_rent_order_analysis_hour", plan.get("tables", []))
        self.assertIn("new_rent_counts", plan.get("measures", []))

    def test_execute_route_without_tables_falls_back_to_respond(self):
        """Planner 判定 execute 但既无表信息也无法构建方案时，respond 澄清（不再降级 Advisor）。"""
        planner_kwargs = _planner_kwargs(
            route="execute",
            effective_query="查询昨天的续租率",
            tables=[],
            fields=[],
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
        self.assertEqual(result["route"], "respond")
        self.assertIsNone(result.get("confirmed_plan"))

    def test_react_tool_loop_feeds_tool_result(self):
        """Planner ReAct：LLM 调用 search_columns 后，工具结果以 ToolMessage 回填进最终结构化输入。"""
        seen = []
        planner_kwargs = _planner_kwargs()
        self._run_planner(
            ["调出"],
            planner_kwargs,
            react_tool_calls=[{
                "name": "search_columns",
                "args": {"question": "山东瀛能", "table": "ads_trip.ads_gundam_device_transfer_detail_hour"},
                "id": "call_1",
            }],
            seen_messages=seen,
        )
        self.assertTrue(seen, "结构化 LLM 应收到 messages")
        tool_msgs = [m for m in seen[0] if getattr(m, "type", "") == "tool"]
        self.assertTrue(tool_msgs, "工具结果应以 ToolMessage 回填")
        self.assertIn("search_columns", tool_msgs[0].content)


if __name__ == "__main__":
    unittest.main()
