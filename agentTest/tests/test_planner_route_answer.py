# Planner route=respond 测试（自由文本模式）：Planner 直接输出文本给用户（澄清/确认/最终回答由 LLM 自定）
# 覆盖：ReAct 轮自由文本即最终回答、query 改写 effective_query、空文本通用兜底、消息入历史
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage
from agentTest.langgraph_app.prompts.planner_prompt import (
    REWRITE_SYSTEM_PROMPT,
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
    """自由文本模式 Planner 的 LLM 桩：
    invoke 首次（rewrite_llm）返回 effective_query 文本，后续 invoke（chat_openai 兜底）返回 fallback_text；
    bind_tools 返回 _FakeReactLLM 驱动 ReAct 工具循环。
    """

    def __init__(self, effective_query="", react_calls=None, fallback_text=""):
        self._effective_query = effective_query
        self._react = _FakeReactLLM(react_calls or [])
        self._fallback_text = fallback_text
        self._invoke_count = 0
        self.rewrite_messages = None
        self.thinking_calls = []

    def invoke(self, messages):
        self._invoke_count += 1
        if self._invoke_count == 1:
            # rewrite_llm：返回改写后的 effective_query 自由文本
            self.rewrite_messages = messages
            return AIMessage(content=self._effective_query)
        # 撞 MAX_PLANNER_TOOL_STEPS 上限后的 thinking 定稿
        self.thinking_calls.append(messages)
        return AIMessage(content=self._fallback_text)

    def bind_tools(self, tools):
        return self._react


def _build_runtime():
    from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
    from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec
    registry = ToolRegistry()
    for name in ("search_databases", "search_tables", "search_columns", "query_stored_result", "probe_values", "grep_semantic"):
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
        "request_id": "req-answer",
    }
    state.update(overrides)
    return state


class PlannerRouteRespondTest(unittest.TestCase):
    """Planner route=respond：直接输出文本给用户（澄清/确认/最终回答）。"""

    def _run_planner(self, effective_query="", react_calls=None, messages=None, fallback_text=""):
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeLLM(effective_query=effective_query, react_calls=react_calls, fallback_text=fallback_text)
        with mock.patch.object(planner_node, "ThinkingStreamChatModel", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state("查询徐州大区今年同意返厂的返厂明细", messages)), fake_llm

    def test_respond_route_returns_text(self):
        """react 轮自由文本即最终回答：直接返回文本并结束本轮。"""
        result, _ = self._run_planner(react_calls=["已确认：2026 年徐州大区没有同意返厂的返厂记录。"])
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

    def test_effective_query_rewritten(self):
        """query 改写：rewrite_llm 输出作为 effective_query 落盘/展示。"""
        result, fake_llm = self._run_planner(
            effective_query="查询徐州大区今年同意返厂的返厂明细（改写后）",
            react_calls=["已确认无匹配数据。"],
        )
        self.assertIn("改写后", result["effective_query"])
        # rewrite 调用收到 REWRITE_SYSTEM_PROMPT
        self.assertIsNotNone(fake_llm.rewrite_messages)
        self.assertIn(REWRITE_SYSTEM_PROMPT, fake_llm.rewrite_messages[0].content)

    def test_rewrite_failure_falls_back_to_raw_input(self):
        """query 改写异常：沿用本轮原始输入，不影响主流程。"""
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeLLM(effective_query="", react_calls=["直接回答。"])
        real_invoke = fake_llm.invoke

        def broken_invoke(messages):
            raise RuntimeError("改写服务不可用")

        fake_llm.invoke = broken_invoke
        with mock.patch.object(planner_node, "ThinkingStreamChatModel", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            result = node(_state("查询徐州大区返厂明细"))
        self.assertEqual(result["effective_query"], "查询徐州大区返厂明细")
        self.assertIn("直接回答", result["final_answer"])

    def test_empty_text_generic_fallback(self):
        """react 与 thinking 定稿均空文本：通用占位，避免空回复。"""
        result, fake_llm = self._run_planner(react_calls=[""], fallback_text="")
        self.assertEqual(result["route"], "respond")
        self.assertEqual(result["final_answer"], "查询遇到问题，请稍后重试。")
        self.assertTrue(fake_llm.thinking_calls, "空文本应触发一次 thinking 定稿")

    def test_respond_message_in_history_context(self):
        """Planner respond 的消息应进入对话历史（供后续追问）。"""
        from agentTest.langgraph_app.nodes.planner_node import _build_history_context
        messages = [
            HumanMessage(content="查询徐州大区返厂明细", id="u1"),
            AIMessage(content="已确认无数据。", name="planner", id="r1:respond"),
        ]
        ctx = _build_history_context(messages)
        self.assertIn("已确认无数据", ctx)

    def test_first_turn_history_excludes_current_input(self):
        """首轮无历史：排除本轮 user 消息（{request_id}:user）后，对话历史应为空。"""
        from agentTest.langgraph_app.nodes.planner_node import _build_history_context
        messages = [HumanMessage(content="查询昨天租赁中的订单数", id="req-first:user")]
        ctx = _build_history_context(messages, exclude_user_id="req-first:user")
        self.assertEqual(ctx.strip(), "", "首轮不应把本轮输入当作对话历史")

    def test_multi_turn_history_excludes_current_only(self):
        """多轮：排除本轮 user 后，只保留真正的历史（上一轮 user + 上一轮回答）。"""
        from agentTest.langgraph_app.nodes.planner_node import _build_history_context
        messages = [
            HumanMessage(content="查询昨天新增订单数", id="u1:user"),
            AIMessage(content="昨天新增订单 100 单。", name="planner", id="r1:respond"),
            HumanMessage(content="那租赁中订单数呢", id="u2:user"),
        ]
        ctx = _build_history_context(messages, exclude_user_id="u2:user")
        self.assertNotIn("那租赁中订单数呢", ctx, "本轮输入不应出现在历史中")
        self.assertIn("查询昨天新增订单数", ctx)
        self.assertIn("昨天新增订单 100 单", ctx)


if __name__ == "__main__":
    unittest.main()
