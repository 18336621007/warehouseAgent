# Planner route=answer 测试：Planner 直接回答能力（查/问/答三选一，无 0 行专用字段）
# 覆盖：answer 分支生成 final_answer 并结束、空 final_answer 回退 advisor、消息入历史
import unittest
from unittest import mock

from langchain_core.messages import AIMessage
from agentTest.langgraph_app.prompts.planner_prompt import (
    PlannerOutput,
    SemanticKeywordsOutput,
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
    for name in ("search_databases", "search_tables", "search_columns", "query_stored_result", "probe_values"):
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
        "request_id": "req-answer",
    }


def _planner_kwargs(**overrides):
    base = {
        "effective_query": "查询徐州大区今年同意返厂的返厂明细",
        "route": "answer",
        "final_answer": "已确认：2026 年徐州大区没有同意返厂的返厂记录。",
        "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
        "fields": [],
        "completeness": "full",
        "complex": False,
        "metric_mentions": ["返厂明细"],
        "dimension_mentions": ["徐州大区"],
        "analysis_type": "detail",
        "reason": "0 行反馈后 probe_values 确认无匹配数据，直接告知用户",
        "semantic_keywords": ["返厂", "明细"],
        "semantic_metrics": [],
    }
    base.update(overrides)
    return base


class PlannerRouteAnswerTest(unittest.TestCase):
    """Planner route=answer：直接回答能力。"""

    def _run_planner(self, planner_kwargs, messages=None):
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeStructuredLLM(["返厂", "明细"], planner_kwargs)
        with mock.patch.object(planner_node, "ChatOpenAI", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state("查询徐州大区今年同意返厂的返厂明细", messages))

    def test_answer_route_returns_final_answer(self):
        """route=answer + final_answer 非空：直接返回最终答复并结束本轮。"""
        result = self._run_planner(_planner_kwargs())
        self.assertEqual(result["route"], "answer")
        self.assertEqual(result["topic_status"], "completed")
        self.assertIn("没有同意返厂的返厂记录", result["final_answer"])
        # 最终答复写入消息（id 以 :answer 结尾，供历史过滤）
        msg = result["messages"][0]
        self.assertIsInstance(msg, AIMessage)
        self.assertTrue(msg.id.endswith(":answer"))
        self.assertEqual(msg.name, "planner")
        # 消费 0 行自愈标记，避免残留
        self.assertFalse(result.get("seeker_empty_result"))

    def test_answer_route_empty_answer_falls_back_advisor(self):
        """route=answer 但 final_answer 为空：回退 advisor 澄清，避免空回复。"""
        result = self._run_planner(_planner_kwargs(final_answer=""))
        self.assertEqual(result["route"], "advisor")
        self.assertIn("answer 但未给出 final_answer", result.get("planner_reason", ""))
        self.assertNotIn("final_answer", result)

    def test_answer_message_in_history_context(self):
        """Planner 直接回答的消息应进入对话历史（供后续追问）。"""
        from agentTest.langgraph_app.nodes.planner_node import _build_history_context
        from langchain_core.messages import HumanMessage
        messages = [
            HumanMessage(content="查询徐州大区返厂明细", id="u1"),
            AIMessage(content="已确认无数据。", name="planner", id="r1:answer"),
        ]
        ctx = _build_history_context(messages)
        self.assertIn("已确认无数据", ctx)


if __name__ == "__main__":
    unittest.main()
