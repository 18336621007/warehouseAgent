
# 0 行自愈测试：Seeker 执行成功但无数据时，未达上限回 Planner 自愈，达上限如实告知无数据
# 覆盖：build_final_answer 的 empty_self_heal / empty_result 分支、route_after_seeker 0 行路由
import unittest
from unittest import mock

from agentTest.config.planner import MAX_EMPTY_RESULT_ROUNDS
from agentTest.langgraph_app.nodes.build_final_answer_node import build_build_final_answer_node
from agentTest.langgraph_app.routers.seeker_router import route_after_seeker


class _FakeLLM:
    # 非 0 行走成功分支时需要 LLM 整理答案，这里只做桩
    def invoke(self, prompt_value):
        return "查询完成，共 1 行数据。"


def _state(**overrides):
    base = {
        "request_id": "req-empty",
        "conversation_id": "conv-empty",
        "effective_query": "查询徐州大区返厂明细",
        "current_user_input": "查询徐州大区返厂明细",
        "sql_valid": True,
        "sql_result": {"columns": ["region_name"], "rows": [], "row_count": 0},
        "sql_exec_failed": False,
        "confirmed_plan": {},
    }
    base.update(overrides)
    return base


class EmptyResultSelfHealTest(unittest.TestCase):
    """build_final_answer 0 行分支：上限内自愈，上限外告知无数据。"""

    def _run_node(self, state, llm=None):
        with mock.patch(
            "agentTest.langgraph_app.nodes.build_final_answer_node.save_query_result",
            return_value={},
        ):
            node = build_build_final_answer_node({"llm": llm})
            return node(state)

    def test_zero_rows_below_limit_triggers_self_heal(self):
        """未达重试上限：设置 seeker_empty_result，不直接回复"无数据"。"""
        out = self._run_node(_state(empty_result_rounds=0))
        self.assertTrue(out.get("seeker_empty_result"))
        self.assertEqual(out.get("empty_result_rounds"), 1)
        self.assertNotIn("final_answer", out)
        self.assertEqual(out.get("topic_status"), "generating_sql")

    def test_zero_rows_reaches_limit_returns_no_data(self):
        """达到重试上限：如实告知无数据，结束本轮。"""
        out = self._run_node(_state(empty_result_rounds=MAX_EMPTY_RESULT_ROUNDS))
        self.assertFalse(out.get("seeker_empty_result"))
        self.assertIn("没有查询到符合条件的数据", out.get("final_answer", ""))
        self.assertEqual(out.get("topic_status"), "completed")

    def test_non_empty_result_not_self_heal(self):
        """有数据时不触发 0 行自愈（走正常成功分支，需 LLM 整理答案）。"""
        state = _state(
            sql_result={"columns": ["region_name"], "rows": [{"region_name": "徐州大区"}], "row_count": 1},
        )
        out = self._run_node(state, llm=_FakeLLM())
        self.assertFalse(out.get("seeker_empty_result"))
        self.assertIn("final_answer", out)
        self.assertEqual(out.get("topic_status"), "completed")


class SeekerEmptyRoutingTest(unittest.TestCase):
    """route_after_seeker：seeker_empty_result 时回 Planner 自愈。"""

    def test_empty_result_goes_repair(self):
        state = {"seeker_empty_result": True}
        self.assertEqual(route_after_seeker(state), "repair")

    def test_empty_result_precedes_plan_error(self):
        # 0 行自愈优先于方案错误修复（两者不会同时出现，顺序兜底）
        state = {"seeker_empty_result": True, "seeker_plan_error": "x"}
        self.assertEqual(route_after_seeker(state), "repair")


if __name__ == "__main__":
    unittest.main()
