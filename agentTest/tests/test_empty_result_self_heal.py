# 0 行自愈测试（A1）：persist_result 执行成功但无数据时，未达上限回 Agent 自愈，达上限回 Agent 告知；
# 有数据时写入结果快照（供 execute_query 工具回填给 Agent 撰写回答）
# 覆盖：persist_result 的 empty_self_heal / empty_result / review 分支
import unittest
import unittest.mock

from agentTest.config.planner import MAX_EMPTY_RESULT_ROUNDS
from agentTest.langgraph_app.nodes.persist_result_node import persist_result_node


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
    """persist_result 0 行分支：上限内自愈，上限外回 Agent 告知，有数据写入结果快照。"""

    def _run_node(self, state):
        with unittest.mock.patch(
            "agentTest.langgraph_app.nodes.persist_result_node.save_query_result",
            return_value={},
        ):
            return persist_result_node(state)

    def test_zero_rows_below_limit_triggers_self_heal(self):
        """未达重试上限：设置 seeker_empty_result，回 Agent 自愈。"""
        out = self._run_node(_state(empty_result_rounds=0))
        self.assertTrue(out.get("seeker_empty_result"))
        self.assertEqual(out.get("empty_result_rounds"), 1)
        self.assertEqual(out.get("topic_status"), "generating_sql")

    def test_zero_rows_reaches_limit_still_goes_planner(self):
        """达到重试上限：仍回 Agent（由 Agent 依据上限提示直接告知无数据）。"""
        out = self._run_node(_state(empty_result_rounds=MAX_EMPTY_RESULT_ROUNDS))
        self.assertTrue(out.get("seeker_empty_result"))
        self.assertEqual(out.get("empty_result_rounds"), MAX_EMPTY_RESULT_ROUNDS + 1)

    def test_non_empty_result_writes_snapshot(self):
        """有数据时写入结果快照（引用+预览+CSV），供 Agent 撰写回答。"""
        state = _state(
            sql_result={"columns": ["region_name"], "rows": [{"region_name": "徐州大区"}], "row_count": 1},
        )
        out = self._run_node(state)
        self.assertFalse(out.get("seeker_empty_result"))
        self.assertEqual(out.get("topic_status"), "executing")
        # 结果快照写入，供 Agent 评审注入
        self.assertEqual(out.get("last_query_result")["row_count"], 1)
        self.assertEqual(out.get("result_preview")[0]["region_name"], "徐州大区")
        self.assertEqual(out.get("result_id"), "req-empty:result")


if __name__ == "__main__":
    unittest.main()
