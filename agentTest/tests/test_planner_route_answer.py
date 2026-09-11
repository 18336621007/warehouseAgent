# Planner route=respond 测试：Planner 直接输出文本给用户（澄清/确认/最终回答由 LLM 自定）
# 覆盖：respond 分支生成 respond_text 并结束、空 respond_text 回退通用引导、消息入历史
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


def _state(user_input, messages=None, **overrides):
    state = {
        "current_user_input": user_input,
        "messages": messages or [],
        "confirmed_plan": {},
        "analysis_spec": {},
        "request_id": "req-answer",
    }
    state.update(overrides)
    return state


def _planner_kwargs(**overrides):
    base = {
        "effective_query": "查询徐州大区今年同意返厂的返厂明细",
        "route": "respond",
        "respond_text": "已确认：2026 年徐州大区没有同意返厂的返厂记录。",
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


class PlannerRouteRespondTest(unittest.TestCase):
    """Planner route=respond：直接输出文本给用户（澄清/确认/最终回答）。"""

    def _run_planner(self, planner_kwargs, messages=None, state_overrides=None):
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeStructuredLLM(["返厂", "明细"], planner_kwargs)
        with mock.patch.object(planner_node, "ChatOpenAI", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state("查询徐州大区今年同意返厂的返厂明细", messages, **(state_overrides or {})))

    def test_respond_route_returns_text(self):
        """route=respond + respond_text 非空：直接返回文本并结束本轮。"""
        result = self._run_planner(_planner_kwargs())
        self.assertEqual(result["route"], "respond")
        self.assertEqual(result["topic_status"], "clarifying")
        self.assertIn("没有同意返厂的返厂记录", result["final_answer"])
        # 文本写入消息（id 以 :respond 结尾，供历史过滤）
        msg = result["messages"][0]
        self.assertIsInstance(msg, AIMessage)
        self.assertTrue(msg.id.endswith(":respond"))
        self.assertEqual(msg.name, "planner")
        # 消费 0 行自愈标记，避免残留
        self.assertFalse(result.get("seeker_empty_result"))
        # 非执行回看的 respond 不触发 Evaluator
        self.assertFalse(result.get("evaluator_pending"))

    def test_respond_route_empty_text_falls_back_guidance(self):
        """route=respond 但 respond_text 为空（回答未完成）：回退通用引导语，不暴露内部 reason。"""
        result = self._run_planner(_planner_kwargs(respond_text=""))
        self.assertEqual(result["route"], "respond")
        self.assertEqual(
            result["final_answer"],
            "请补充最关键的指标、维度或过滤条件，我好继续为您查询。",
        )

    def test_respond_message_in_history_context(self):
        """Planner respond 的消息应进入对话历史（供后续追问）。"""
        from agentTest.langgraph_app.nodes.planner_node import _build_history_context
        from langchain_core.messages import HumanMessage
        messages = [
            HumanMessage(content="查询徐州大区返厂明细", id="u1"),
            AIMessage(content="已确认无数据。", name="planner", id="r1:respond"),
        ]
        ctx = _build_history_context(messages)
        self.assertIn("已确认无数据", ctx)

    def test_execute_route_builds_plan(self):
        """route=execute 且方案构建成功：进执行链（route=execute），写入 confirmed_plan/plans。"""
        result = self._run_planner(_planner_kwargs(
            route="execute",
            effective_query="查询昨天的新增订单数",
            dimension_mentions=[],
            filters="pt_dt 昨天",
            semantic_metrics=[
                {
                    "id": "addition_order_num",
                    "confidence": 0.95,
                    "mention": "新增订单",
                }
            ],
        ))
        self.assertEqual(result["route"], "execute")
        self.assertEqual(result["topic_status"], "confirmed")
        self.assertTrue(result.get("confirmed_plan"))
        self.assertEqual(len(result.get("plans") or []), 1)
        self.assertEqual(result.get("execution_rounds"), 1)


if __name__ == "__main__":
    unittest.main()
