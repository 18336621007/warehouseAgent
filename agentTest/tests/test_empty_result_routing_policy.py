# -*- coding: utf-8 -*-
# 0 行自愈路由策略测试（自由文本模式）：查数返回 0 行由 Planner 自主判断——probe_values 探查 / 修正 filters 重查 / 确认无数据直接告知
# 覆盖：prompt 0 行规则、probe_values→execute_query 重跑链路、直接告知无数据
import unittest
from unittest import mock

from langchain_core.messages import AIMessage
from agentTest.langgraph_app.prompts.planner_prompt import PLANNER_SYSTEM_PROMPT


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
    """ReAct 桩：按预设序列返回 tool_call 或自由文本；序列耗尽返回空文本。"""

    def __init__(self, react_calls):
        self._calls = list(react_calls)
        self._step = 0
        self.seen_messages = []

    def invoke(self, messages):
        self.seen_messages.append(messages)
        if self._step < len(self._calls):
            item = self._calls[self._step]
            self._step += 1
            if isinstance(item, dict):  # tool_call
                return AIMessage(content="", tool_calls=[item])
            return AIMessage(content=item)  # 自由文本回答
        return AIMessage(content="")


class _FakeLLM:
    """自由文本模式 Planner 的 LLM 桩：invoke 首次（rewrite）返回 effective_query，
    后续 invoke（chat_openai 兜底）返回 fallback_text；bind_tools 返回 _FakeReactLLM。"""

    def __init__(self, effective_query="", react_calls=None, fallback_text=""):
        self._effective_query = effective_query
        self._react = _FakeReactLLM(react_calls or [])
        self._fallback_text = fallback_text
        self._invoke_count = 0

    def invoke(self, messages):
        self._invoke_count += 1
        if self._invoke_count == 1:
            return AIMessage(content=self._effective_query)
        return AIMessage(content=self._fallback_text)

    def bind_tools(self, tools):
        return self._react


def _build_runtime():
    from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
    from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec
    registry = ToolRegistry()
    for name in ("search_databases", "search_tables", "search_columns", "query_stored_result", "probe_values", "grep_semantic", "execute_query"):
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
        "request_id": "req-empty-routing",
    }
    base.update(overrides)
    return base


class EmptyResultRoutingPolicyTest(unittest.TestCase):
    """0 行自愈路由策略：事实问题由 Planner 自主探查/重查/告知，不再由程序强转。"""

    def _run(self, react_calls, user_input="查询徐州大区今年同意返厂的返厂明细", fallback_text="", **state_overrides):
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeLLM(effective_query="查询徐州大区今年同意返厂的返厂明细", react_calls=react_calls, fallback_text=fallback_text)
        with mock.patch.object(planner_node, "ThinkingStreamChatModel", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state(user_input, **state_overrides)), fake_llm

    def test_prompt_guides_zero_row_self_heal(self):
        # prompt 引导：0 行时用 probe_values 核实实际取值，或确认确属无数据后直接告知
        self.assertIn("probe_values", PLANNER_SYSTEM_PROMPT)
        self.assertIn("0 行", PLANNER_SYSTEM_PROMPT)

    def test_zero_row_probe_then_execute(self):
        """0 行自愈：react 先调 probe_values 探查，再调 execute_query 重查，最后自由文本回答。"""
        react_calls = [
            {
                "name": "probe_values",
                "args": {"table": "ads_trip.ads_gundam_device_return_detail_hour", "column": "region_name", "keyword": "徐州", "limit": 10},
                "id": "call_probe_1",
            },
            {
                "name": "execute_query",
                "args": {"question": "查询徐州大区今年同意返厂的返厂明细", "filters": "region_name='徐州大区' AND status='同意返厂' AND create_time >= '2026-01-01' AND create_time <= '2026-12-31'"},
                "id": "call_exec_1",
            },
            "探查确认实际存储为「徐州大区」后已按修正过滤条件重查。",
        ]
        result, fake_llm = self._run(react_calls)
        self.assertEqual(result.get("route"), "respond")
        self.assertEqual(result.get("topic_status"), "clarifying")
        self.assertIn("已按修正过滤条件重查", result.get("final_answer", ""))

    def test_zero_row_direct_answer(self):
        """确认确属无数据：Planner 直接告知用户，不再反复重试。"""
        result, _ = self._run(["核实后确认 2026 年徐州大区没有同意返厂的返厂记录。"])
        self.assertEqual(result.get("route"), "respond")
        self.assertIn("没有同意返厂的返厂记录", result.get("final_answer", ""))


if __name__ == "__main__":
    unittest.main()
