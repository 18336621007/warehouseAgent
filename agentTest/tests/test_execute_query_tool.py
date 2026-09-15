# execute_query 工具单元测试：把 Seeker 执行链封装为一次工具调用
# 覆盖：metric_id 解析 / grep 兜底 / 结果摘要（成功预览、0 行提示、方案不可行、未命中指标）
import unittest
from unittest import mock

from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider
from agentTest.metadata.semantic_metadata_provider import SemanticMetadataProvider
from agentTest.langgraph_app.tools.execute_query_tool import (
    build_execute_query_tool,
    set_execute_query_context,
    reset_execute_query_context,
)


class _FakeSeekerGraph:
    """Seeker 子图桩：记录入参 state，返回预设结果状态。"""

    def __init__(self, result_state):
        self._result_state = result_state
        self.calls = []

    def invoke(self, state):
        self.calls.append(state)
        return self._result_state


def _runtime():
    provider = SemanticMetadataProvider(get_semantic_layer_provider())
    return {"semantic_metadata_provider": provider}


def _success_result():
    return {
        "seeker_plan_error": "",
        "sql_exec_failed": False,
        "sql_result": {
            "columns": ["company_name", "new_rent_counts"],
            "row_count": 2,
        },
        "result_preview": [
            {"company_name": "科斯特", "new_rent_counts": 120},
            {"company_name": "锂纳斯", "new_rent_counts": 80},
        ],
        "result_id": "rid-1",
        "result_csv": "D:/tmp/conv1/r1.csv",
    }


class ExecuteQueryToolTest(unittest.TestCase):
    """查数工具：语义层方案构建 → Seeker 子图执行 → 结果摘要回填 Agent。"""

    def setUp(self):
        self._tokens = set_execute_query_context("req-1", "conv1", "topic-1")

    def tearDown(self):
        reset_execute_query_context(self._tokens)

    def test_metric_id_resolves_and_invokes_seeker(self):
        """按 metric_id 定位指标：构建 confirmed_plan 并调用 Seeker 子图。"""
        seeker = _FakeSeekerGraph(_success_result())
        tool = build_execute_query_tool(_runtime(), seeker)
        out = tool.invoke({
            "question": "查询昨天的新增订单数",
            "metric_id": "addition_order_num",
            "filters": "pt_dt = '2026-09-13'",
            "dimensions": "company_name",
        })
        self.assertEqual(len(seeker.calls), 1)
        state = seeker.calls[0]
        plan = state.get("confirmed_plan") or {}
        self.assertEqual(plan.get("status"), "confirmed")
        self.assertIn("new_rent_counts", plan.get("measures", []))
        # 返回文本前置语义层命中行，便于 LLM/日志审计实际走的指标
        self.assertIn("已按语义层指标", out)
        self.assertIn("addition_order_num", out)
        self.assertIn("查询成功", out)
        self.assertIn("科斯特", out)
        self.assertEqual(state.get("request_id"), "req-1")
        self.assertEqual(state.get("conversation_id"), "conv1")

    def test_grep_fallback_when_no_metric_id(self):
        """未给 metric_id 时按问题词（空格/标点分隔）grep 兜底定位指标。"""
        seeker = _FakeSeekerGraph(_success_result())
        tool = build_execute_query_tool(_runtime(), seeker)
        out = tool.invoke({"question": "新增订单 昨天"})
        self.assertEqual(len(seeker.calls), 1)
        plan = seeker.calls[0].get("confirmed_plan") or {}
        self.assertIn("new_rent_counts", plan.get("measures", []))
        self.assertIn("查询成功", out)

    def test_no_metric_hit_returns_guidance(self):
        """完全未命中语义层指标：返回引导文案，不调用 Seeker。"""
        seeker = _FakeSeekerGraph(_success_result())
        tool = build_execute_query_tool(_runtime(), seeker)
        out = tool.invoke({"question": "请问今天天气如何"})
        self.assertIn("未匹配到语义层指标", out)
        self.assertEqual(len(seeker.calls), 0)

    def test_zero_row_note_in_summary(self):
        """0 行结果：摘要中提示过滤值与实际存储值可能不一致。"""
        seeker = _FakeSeekerGraph({
            "seeker_plan_error": "",
            "sql_exec_failed": False,
            "sql_result": {"columns": ["company_name"], "row_count": 0},
            "result_preview": [],
            "result_id": "rid-0",
            "result_csv": "",
        })
        tool = build_execute_query_tool(_runtime(), seeker)
        out = tool.invoke({"question": "查询昨天的新增订单数", "metric_id": "addition_order_num"})
        self.assertIn("返回 0 行", out)

    def test_plan_error_summary(self):
        """方案构建不可行：摘要返回失败原因。"""
        seeker = _FakeSeekerGraph({"seeker_plan_error": "缺少 join 契约", "sql_exec_failed": False})
        tool = build_execute_query_tool(_runtime(), seeker)
        out = tool.invoke({"question": "查询昨天的新增订单数", "metric_id": "addition_order_num"})
        self.assertIn("查询方案不可行", out)
        self.assertIn("缺少 join 契约", out)

    def test_exec_failed_summary(self):
        """执行失败：摘要返回错误文本。"""
        seeker = _FakeSeekerGraph({
            "seeker_plan_error": "",
            "sql_exec_failed": True,
            "sql_exec_error": "Hive 连接超时",
        })
        tool = build_execute_query_tool(_runtime(), seeker)
        out = tool.invoke({"question": "查询昨天的新增订单数", "metric_id": "addition_order_num"})
        self.assertIn("查询执行失败", out)
        self.assertIn("Hive 连接超时", out)

    def test_metric_hit_logged_with_source_and_tier(self):
        """工具内部命中语义层时记录 semantic.match（source=execute_query_tool），
        即使 Planner 未声明 semantic_metrics 也能从日志审计实际走的语义层路径。"""
        seeker = _FakeSeekerGraph(_success_result())
        tool = build_execute_query_tool(_runtime(), seeker)
        with mock.patch(
            "agentTest.langgraph_app.tools.execute_query_tool.log_metric_event"
        ) as mocked:
            tool.invoke({"question": "新增订单 昨天"})
        self.assertTrue(mocked.called)
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs.get("source"), "execute_query_tool")
        self.assertEqual(kwargs.get("metric_source"), "grep_fallback")
        self.assertEqual(kwargs.get("node_name"), "execute_query")
        self.assertGreaterEqual(kwargs.get("hit_count", 0), 1)
        self.assertIn("addition_order_num", kwargs.get("metric_ids", []))
        # 多候选 grep 命中：unique 或 candidate 均属语义层命中（非 rag）
        self.assertIn(kwargs.get("tier"), ("unique", "candidate"))

    def test_metric_id_hit_logged_as_unique(self):
        """Agent 明确指定 metric_id：日志记为 unique 强命中（metric_source=metric_id）。"""
        seeker = _FakeSeekerGraph(_success_result())
        tool = build_execute_query_tool(_runtime(), seeker)
        with mock.patch(
            "agentTest.langgraph_app.tools.execute_query_tool.log_metric_event"
        ) as mocked:
            tool.invoke({"question": "查询昨天的新增订单数", "metric_id": "addition_order_num"})
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs.get("metric_source"), "metric_id")
        self.assertEqual(kwargs.get("tier"), "unique")
        self.assertIn("addition_order_num", kwargs.get("metric_ids", []))


if __name__ == "__main__":
    unittest.main()
