# 0 行自愈测试（A1）：persist_result 执行成功但无数据时，未达上限回 Planner 自愈，达上限回 Planner 告知；
# 有数据时置 execution_review 回 Planner 撰写最终回答
# 覆盖：persist_result 的 empty_self_heal / empty_result / review 分支、route_after_seeker 路由
import unittest

from agentTest.config.planner import MAX_EMPTY_RESULT_ROUNDS
from agentTest.langgraph_app.nodes.persist_result_node import persist_result_node
from agentTest.langgraph_app.routers.seeker_router import route_after_seeker


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


class PersistResultSelfHealTest(unittest.TestCase):
    """persist_result 0 行分支：上限内自愈，上限外回 Planner 告知，有数据回看撰写回答。"""

    def _run_node(self, state):
        with unittest.mock.patch(
            "agentTest.langgraph_app.nodes.persist_result_node.save_query_result",
            return_value={},
        ):
            return persist_result_node(state)

    def test_zero_rows_below_limit_triggers_self_heal(self):
        """未达重试上限：设置 seeker_empty_result，不回看评审。"""
        out = self._run_node(_state(empty_result_rounds=0))
        self.assertTrue(out.get("seeker_empty_result"))
        self.assertEqual(out.get("empty_result_rounds"), 1)
        self.assertFalse(out.get("execution_review"))
        self.assertFalse(out.get("evaluator_pending"))
        self.assertEqual(out.get("topic_status"), "generating_sql")

    def test_zero_rows_reaches_limit_still_goes_planner(self):
        """达到重试上限：仍回 Planner（由 Planner 依据上限提示直接告知无数据）。"""
        out = self._run_node(_state(empty_result_rounds=MAX_EMPTY_RESULT_ROUNDS))
        self.assertTrue(out.get("seeker_empty_result"))
        self.assertEqual(out.get("empty_result_rounds"), MAX_EMPTY_RESULT_ROUNDS + 1)

    def test_non_empty_result_sets_execution_review(self):
        """有数据时置 execution_review + evaluator_pending，回 Planner 撰写回答并评估。"""
        state = _state(
            sql_result={"columns": ["region_name"], "rows": [{"region_name": "徐州大区"}], "row_count": 1},
        )
        out = self._run_node(state)
        self.assertTrue(out.get("execution_review"))
        self.assertTrue(out.get("evaluator_pending"))
        self.assertFalse(out.get("seeker_empty_result"))
        self.assertEqual(out.get("topic_status"), "executing")
        # 结果快照写入，供 Planner 评审注入
        self.assertEqual(out.get("plan_results")[0]["row_count"], 1)


class SeekerEmptyRoutingTest(unittest.TestCase):
    """route_after_seeker：0 行/回看/正常完成的路由。"""

    def test_empty_result_goes_empty_self_heal(self):
        state = {"seeker_empty_result": True, "execution_review": False}
        self.assertEqual(route_after_seeker(state), "empty_self_heal")

    def test_execution_review_goes_planner(self):
        state = {"execution_review": True, "seeker_empty_result": False}
        self.assertEqual(route_after_seeker(state), "review")

    def test_empty_result_precedes_review(self):
        # 0 行自愈优先于执行回看（两者互斥，顺序兜底）
        state = {"seeker_empty_result": True, "execution_review": True}
        self.assertEqual(route_after_seeker(state), "empty_self_heal")


if __name__ == "__main__":
    unittest.main()
