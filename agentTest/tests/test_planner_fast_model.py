# 方案2 快速模型定稿测试：验证 planner 在"模型停止工具调用"时用 fast 模型定稿，
# fast 失败（respond 文本为空）后回退 thinking 模型；route=execute 已废弃不再回退
import json
import unittest
from unittest import mock

from langchain_core.messages import AIMessage
from agentTest.langgraph_app.prompts.planner_prompt import PlannerOutput, SemanticKeywordsOutput


class _FakeTool:
    def __init__(self, name, result="工具结果"):
        self.name = name
        self.result = result

    def invoke(self, args):
        return f"{self.name} -> {self.result}"


class _FakeReactLLM:
    """ReAct 桩：不调用工具，直接输出指定文本（模型收尾/定稿）。"""

    def __init__(self, content="信息已充分，直接输出判定"):
        self._content = content

    def invoke(self, messages):
        return AIMessage(content=self._content)


class _FakeStructuredCallable:
    def __init__(self, value, seen):
        self._value = value
        self._seen = seen

    def invoke(self, prompt_value):
        self._seen.append(prompt_value)
        return self._value


class _FakeLLM:
    """区分 thinking/fast 的 LLM 桩：第一次 with_structured_output 为 thinking，
    之后为 fast；各自独立记录调用。"""

    def __init__(self, thinking_out, fast_out, react_content="信息已充分，直接输出判定"):
        self._thinking_out = thinking_out
        self._fast_out = fast_out
        self._react_content = react_content
        self._so_count = 0
        self.thinking_calls = []
        self.fast_calls = []

    def with_structured_output(self, model, **kwargs):
        self._so_count += 1
        if model is SemanticKeywordsOutput:
            return _FakeStructuredCallable(
                SemanticKeywordsOutput(semantic_keywords=[]), []
            )
        if self._so_count == 1:
            return _FakeStructuredCallable(self._thinking_out, self.thinking_calls)
        return _FakeStructuredCallable(self._fast_out, self.fast_calls)

    def bind_tools(self, tools):
        return _FakeReactLLM(self._react_content)


def _build_runtime():
    from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
    from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec
    registry = ToolRegistry()
    for name in ("search_databases", "search_tables", "search_columns", "query_stored_result", "probe_values", "search_semantic"):
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
        "analysis_spec": {},
        "request_id": "req-fast",
    }


def _planner_kwargs(**overrides):
    base = {
        "effective_query": "查询徐州大区今年同意返厂的返厂明细",
        "route": "respond",
        "respond_text": "已查询：徐州大区 2026 年同意返厂明细共 26 条。",
        "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
        "fields": ["region_name", "status"],
        "completeness": "full",
        "complex": False,
        "metric_mentions": ["返厂明细"],
        "dimension_mentions": ["徐州大区"],
        "analysis_type": "detail",
        "reason": "工具已充分，直接回答",
        "semantic_keywords": ["返厂", "明细"],
        "semantic_metrics": [],
    }
    base.update(overrides)
    return base


class PlannerFastModelTest(unittest.TestCase):
    """方案2：信息充分时用快速模型定稿；失败回退 thinking。"""

    def _run(self, fake_llm):
        from agentTest.langgraph_app.nodes import planner_node
        with mock.patch.object(planner_node, "ThinkingStreamChatModel", return_value=fake_llm):
            node = planner_node.build_planner_node(_build_runtime())
            return node(_state("查询徐州大区今年同意返厂的返厂明细"))

    def test_fast_model_used_when_no_tool_call(self):
        """模型无工具调用直接定稿：走 fast 模型，thinking 不被调用。"""
        fake_llm = _FakeLLM(
            PlannerOutput(**_planner_kwargs()),
            PlannerOutput(**_planner_kwargs()),
        )
        result = self._run(fake_llm)
        self.assertEqual(result["route"], "respond")
        self.assertTrue(fake_llm.fast_calls, "应使用快速模型定稿")
        self.assertEqual(len(fake_llm.thinking_calls), 0, "无需 thinking 模型")

    def test_fast_execute_defensive_respond_fallback(self):
        """fast 输出 route=execute（已废弃终态）：不再拦截重试/回退 thinking，直接防御兜底 respond。"""
        fake_llm = _FakeLLM(
            # thinking 输出：最终回答（不会用到）
            PlannerOutput(**_planner_kwargs()),
            # fast 输出：误判 execute（reason 作为兜底回复）
            PlannerOutput(**_planner_kwargs(route="execute", respond_text="", reason="需要查询徐州大区返厂明细")),
        )
        result = self._run(fake_llm)
        self.assertEqual(result["route"], "respond")
        self.assertTrue(fake_llm.fast_calls, "fast 应先被调用")
        self.assertEqual(len(fake_llm.thinking_calls), 0, "execute 已废弃，不再回退 thinking")
        self.assertIn("需要查询徐州大区返厂明细", result["respond_text"], "防御兜底应使用 reason 作为回复")

    def test_react_direct_json_skips_finalize(self):
        """react 轮直接输出合法 PlannerOutput JSON：直接解析使用，不再调 fast/thinking 定稿。"""
        json_text = json.dumps(PlannerOutput(**_planner_kwargs()).model_dump(), ensure_ascii=False)
        fake_llm = _FakeLLM(
            PlannerOutput(**_planner_kwargs()),
            PlannerOutput(**_planner_kwargs()),
            react_content=json_text,
        )
        result = self._run(fake_llm)
        self.assertEqual(result["route"], "respond")
        self.assertEqual(len(fake_llm.fast_calls), 0, "直接解析成功，无需 fast 定稿")
        self.assertEqual(len(fake_llm.thinking_calls), 0, "直接解析成功，无需 thinking 定稿")

    def test_fast_empty_text_falls_back_to_thinking(self):
        """fast 输出 respond 但 respond_text 为空：重试后回退 thinking 定稿。"""
        fake_llm = _FakeLLM(
            PlannerOutput(**_planner_kwargs()),
            PlannerOutput(**_planner_kwargs(route="respond", respond_text="")),
        )
        result = self._run(fake_llm)
        self.assertEqual(result["route"], "respond")
        self.assertTrue(fake_llm.fast_calls)
        self.assertTrue(fake_llm.thinking_calls)


if __name__ == "__main__":
    unittest.main()
