# Planner ReAct 工具循环测试（自由文本模式）：mock LLM + 桩工具，
# 覆盖：grep_semantic 检索回填、工具结果以 ToolMessage 进入上下文、同参数去重、自由文本最终回答
import unittest
from unittest import mock

from langchain_core.messages import AIMessage


class _FakeVectorStore:
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
    """ReAct 循环桩：按预设序列返回 tool_call 或自由文本；序列耗尽返回空文本。"""

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
    state = {
        "current_user_input": user_input,
        "messages": messages or [],
        "confirmed_plan": {},
        "request_id": "req-grep",
    }
    state.update(overrides)
    return state


class PlannerSemanticGrepFlowTest(unittest.TestCase):
    """Planner ReAct 工具循环：自主检索语义层/元数据并基于工具结果写回答。"""

    def _run(self, react_calls, user_input="查询昨天从山东瀛能公司调出的调出明细"):
        from agentTest.langgraph_app.nodes import planner_node
        fake_llm = _FakeLLM(effective_query="查询昨天从山东瀛能公司调出的调出明细", react_calls=react_calls)
        with mock.patch.object(planner_node, "ThinkingStreamChatModel", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state(user_input)), fake_llm

    def test_react_tool_loop_feeds_tool_result(self):
        """ReAct：LLM 调 grep_semantic 后，工具结果以 ToolMessage 回填进上下文，最终自由文本回答。"""
        react_calls = [
            {"name": "grep_semantic", "args": {"question": "调出明细"}, "id": "call_semantic_1"},
            "已查询到调出明细，请查看。",
        ]
        result, fake_llm = self._run(react_calls)
        self.assertEqual(result["route"], "respond")
        self.assertIn("调出明细", result["final_answer"])
        # 工具结果应作为 ToolMessage 进入下一轮 React 输入
        all_msgs = [m for batch in fake_llm._react.seen_messages for m in batch]
        tool_msgs = [m for m in all_msgs if getattr(m, "type", "") == "tool"]
        self.assertTrue(tool_msgs, "工具结果应以 ToolMessage 回填")
        self.assertIn("grep_semantic", tool_msgs[0].content)

    def test_dedup_same_args_skips_rerun(self):
        """同参数工具调用去重：第二次同参数调用返回提示，不重复注入全量结果。"""
        react_calls = [
            {"name": "grep_semantic", "args": {"question": "调出明细"}, "id": "call_semantic_1"},
            {"name": "grep_semantic", "args": {"question": "调出明细"}, "id": "call_semantic_2"},
            "已基于语义层结果回答。",
        ]
        result, fake_llm = self._run(react_calls)
        self.assertEqual(result["route"], "respond")
        # 第二次同参调用应命中去重提示（工具结果不再重复全量注入）
        all_msgs = [m for batch in fake_llm._react.seen_messages for m in batch]
        tool_msgs = [m for m in all_msgs if getattr(m, "type", "") == "tool"]
        self.assertTrue(
            any("同参数已在上文返回" in m.content for m in tool_msgs),
            "同参数第二次调用应返回去重提示",
        )
        self.assertIn("grep_semantic -> 工具结果", tool_msgs[0].content)

    def test_react_direct_answer_without_tools(self):
        """信息充分不调工具：直接自由文本回答结束本轮。"""
        result, _ = self._run(["山东瀛能昨日无调出明细记录。"])
        self.assertEqual(result["route"], "respond")
        self.assertIn("无调出明细记录", result["final_answer"])


if __name__ == "__main__":
    unittest.main()
