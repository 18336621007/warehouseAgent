# Planner 自由文本模式测试：ReAct 轮直接输出最终回答；撞 MAX_PLANNER_TOOL_STEPS 上限用 thinking 定稿；空文本通用兜底
import unittest
from unittest import mock

from langchain_core.messages import AIMessage


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
    invoke 首次（rewrite_llm）返回 effective_query，后续 invoke（chat_openai 撞上限兜底）返回 fallback_text；
    bind_tools 返回 _FakeReactLLM 驱动 ReAct 循环。
    """

    def __init__(self, effective_query="", react_calls=None, fallback_text=""):
        self._effective_query = effective_query
        self._react = _FakeReactLLM(react_calls or [])
        self._fallback_text = fallback_text
        self._invoke_count = 0
        self.thinking_calls = []

    def invoke(self, messages):
        self._invoke_count += 1
        if self._invoke_count == 1:
            return AIMessage(content=self._effective_query)
        # 撞上限后的 thinking 定稿
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
        "table_vector_store": None,
        "column_vector_store": None,
        "bm25_retriever": None,
        "example_vector_store": None,
        "tool_registry": registry,
        "semantic_metadata_provider": SemanticMetadataProvider(),
    }


def _state(user_input):
    return {
        "current_user_input": user_input,
        "messages": [],
        "confirmed_plan": {},
        "request_id": "req-fast",
    }


class PlannerFastModelTest(unittest.TestCase):
    """自由文本模式：ReAct 文本直接采纳；撞上限 thinking 定稿；空文本兜底。"""

    def _run(self, fake_llm, user_input="查询徐州大区今年同意返厂的返厂明细"):
        from agentTest.langgraph_app.nodes import planner_node
        with mock.patch.object(planner_node, "ThinkingStreamChatModel", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state(user_input))

    def test_react_direct_text_is_final_answer(self):
        """react 轮无工具调用且输出回答文本：直接采纳为最终回答，不再调 thinking 定稿（对齐 Codex 自由输出）。"""
        fake_llm = _FakeLLM(effective_query="查询徐州大区返厂明细", react_calls=["查询结果：共 26 条返厂明细。"])
        result = self._run(fake_llm)
        self.assertEqual(result["route"], "respond")
        self.assertIn("查询结果：共 26 条返厂明细", result["final_answer"])
        self.assertEqual(len(fake_llm.thinking_calls), 0, "react 文本直接采纳，无需 thinking 定稿")

    def test_limit_hit_falls_back_to_thinking(self):
        """撞 MAX_PLANNER_TOOL_STEPS 上限仍无回答：用 thinking 主模型定稿一次。"""
        from agentTest.config.planner import MAX_PLANNER_TOOL_STEPS
        # 全部轮次都调用工具（tool_call），序列耗尽后返回空文本 → 循环跑满上限后走 thinking 定稿
        tool_calls = [
            {"name": "grep_semantic", "args": {"question": "返厂"}, "id": f"c{i}"}
            for i in range(MAX_PLANNER_TOOL_STEPS + 2)
        ]
        fake_llm = _FakeLLM(effective_query="查询徐州大区返厂明细", react_calls=tool_calls, fallback_text="thinking 定稿回答。")
        result = self._run(fake_llm)
        self.assertEqual(result["route"], "respond")
        self.assertIn("thinking 定稿回答", result["final_answer"])
        self.assertTrue(fake_llm.thinking_calls, "撞上限后应触发一次 thinking 定稿")

    def test_empty_text_generic_fallback(self):
        """react 空文本 + thinking 定稿空文本：通用占位（不重试，对齐 Codex）。"""
        fake_llm = _FakeLLM(effective_query="查询徐州大区返厂明细", react_calls=[""], fallback_text="")
        result = self._run(fake_llm)
        self.assertEqual(result["route"], "respond")
        self.assertEqual(result["final_answer"], "查询遇到问题，请稍后重试。")
        self.assertTrue(fake_llm.thinking_calls)


if __name__ == "__main__":
    unittest.main()
