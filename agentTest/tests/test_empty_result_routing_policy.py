# -*- coding: utf-8 -*-
# 0 行自愈路由策略测试：事实问题必须由 Planner 探查（probe_values）解决，禁止 respond 向用户询问过滤值
# 覆盖：prompt 0 行规则强化文本、0 行反馈 section 注入、0 行自愈走 probe_values→seeker 重跑链路
import unittest
from unittest import mock

from langchain_core.messages import AIMessage
from agentTest.langgraph_app.prompts.planner_prompt import (
    PLANNER_SYSTEM_PROMPT,
    PlannerOutput,
    SemanticKeywordsOutput,
    SemanticMetricHit,
)


class _FakeVectorStore:
    def similarity_search_with_score(self, question, k, **kwargs):
        return []


class _FakeReranker:
    def retrieve(self, question, top_k):
        return []


class _FakeTool:
    def __init__(self, name, result="工具结果"):
        self.name = name
        self.result = result

    def invoke(self, args):
        return f"{self.name} -> {self.result}"


class _FakeReactLLM:
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
    def __init__(self, keyword_list, planner_kwargs, react_tool_calls=None, seen_messages=None):
        self._keyword_list = keyword_list
        self._planner_kwargs = planner_kwargs
        self._react_tool_calls = react_tool_calls or []
        self._seen_messages = seen_messages

    def invoke(self, prompt_value):
        return self

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
        if self._seen_messages is not None:
            self._seen_messages.append(prompt_value)
        return self._value


def _build_runtime():
    from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
    from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec
    registry = ToolRegistry()
    for name in ("search_databases", "search_tables", "search_columns", "query_stored_result", "probe_values", "search_semantic"):
        registry.register(ToolSpec(name=name, description="stub", tool=_FakeTool(name), groups=("planner",)))
    return {
        "table_vector_store": _FakeVectorStore(),
        "column_vector_store": _FakeVectorStore(),
        "bm25_retriever": None,
        "example_vector_store": None,
        "tool_registry": registry,
        "semantic_metadata_provider": SemanticMetadataProvider(),
    }


def _state(user_input, messages=None, **overrides):
    base = {
        "current_user_input": user_input,
        "messages": messages or [],
        "confirmed_plan": {},
        "analysis_spec": {},
        "request_id": "req-empty-routing",
    }
    base.update(overrides)
    return base


def _planner_kwargs(**overrides):
    base = {
        "effective_query": "查询徐州大区今年同意返厂的返厂明细",
        "route": "execute",
        "respond_text": "",
        "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
        "fields": [],
        "completeness": "full",
        "complex": False,
        "metric_mentions": ["返厂明细"],
        "dimension_mentions": ["徐州大区"],
        "analysis_type": "detail",
        "reason": "0 行反馈后 probe_values 确认实际值，修正 filters 重跑",
        "filters": "region_name='徐州大区' AND status='同意返厂' AND create_time >= '2026-01-01' AND create_time <= '2026-12-31'",
        "semantic_keywords": ["返厂", "明细"],
        "semantic_metrics": [],
    }
    base.update(overrides)
    return base


class EmptyResultRoutingPolicyTest(unittest.TestCase):
    """0 行自愈路由策略：事实问题由 Planner 探查，禁止 respond 向用户询问过滤值。"""

    def test_prompt_requires_probe_values_on_zero_rows(self):
        # 0 行规则强化：可用工具探查、禁止 route=respond 向用户询问、口径歧义才允许澄清
        self.assertIn("可用 probe_values 探查实际取值", PLANNER_SYSTEM_PROMPT)
        self.assertIn("route=execute 重跑", PLANNER_SYSTEM_PROMPT)
        self.assertIn("禁止 route=respond 向用户询问", PLANNER_SYSTEM_PROMPT)
        self.assertIn("口径歧义", PLANNER_SYSTEM_PROMPT)

    def test_zero_row_section_injected_with_probe_guidance(self):
        # 0 行自愈回 planner 时注入探查引导，提示用 probe_values 而不是问用户
        seen = []
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeStructuredLLM(["返厂", "明细"], _planner_kwargs(), seen_messages=seen)
        with mock.patch.object(planner_node, "ChatOpenAI", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            node(_state(
                "查询徐州大区今年同意返厂的返厂明细",
                seeker_empty_result=True,
                generated_sql="SELECT * FROM ads_trip.ads_gundam_device_return_detail_hour WHERE region_name='徐州'",
            ))
        user_content = "".join(str(m) for m in seen)
        self.assertIn("SQL 执行成功但无数据", user_content)
        self.assertIn("probe_values 探查实际取值", user_content)
        self.assertIn("不要 route=respond 向用户询问", user_content)

    def test_zero_row_section_not_injected_without_flag(self):
        # 无 0 行标记时不注入 0 行反馈 section
        seen = []
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeStructuredLLM(["返厂", "明细"], _planner_kwargs(), seen_messages=seen)
        with mock.patch.object(planner_node, "ChatOpenAI", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            node(_state("查询徐州大区今年同意返厂的返厂明细"))
        user_content = "".join(str(m) for m in seen)
        self.assertNotIn("SQL 执行成功但无数据", user_content)

    def test_zero_row_self_heal_probe_then_seeker(self):
        # 0 行自愈链路：react 先调 probe_values 探查，structured 修正 filters 后 route=execute
        react_tool_calls = [
            {
                "name": "search_semantic",
                "args": {"question": "返厂明细"},
                "id": "call_semantic_1",
                "type": "tool_call",
            },
            {
                "name": "probe_values",
                "args": {
                    "table": "ads_trip.ads_gundam_device_return_detail_hour",
                    "column": "region_name",
                    "keyword": "徐州",
                    "limit": 10,
                },
                "id": "call_probe_1",
                "type": "tool_call",
            }
        ]
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeStructuredLLM(
            ["返厂", "明细"],
            _planner_kwargs(semantic_metrics=[
                SemanticMetricHit(
                    id="device_return_detail",
                    confidence=0.95,
                    mention="返厂明细",
                )
            ]),
            react_tool_calls=react_tool_calls,
        )
        with mock.patch.object(planner_node, "ChatOpenAI", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            result = node(_state(
                "查询徐州大区今年同意返厂的返厂明细",
                seeker_empty_result=True,
                generated_sql="SELECT * FROM ads_trip.ads_gundam_device_return_detail_hour WHERE region_name='徐州'",
            ))
        # 探查修正后仍由 Planner 重跑 execute，不向用户澄清
        self.assertEqual(result.get("route"), "execute")
        self.assertNotEqual(result.get("topic_status"), "clarifying")


if __name__ == "__main__":
    unittest.main()
