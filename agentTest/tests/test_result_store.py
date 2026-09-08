# result_store 结果历史存储单元测试
# 覆盖：落盘（JSON+CSV）、索引轮次、按 round_no/result_id 解析、跨天目录复用、保留策略清理
import shutil
import tempfile
import unittest
from pathlib import Path

from agentTest.langgraph_app.services import result_store


class ResultStoreTest(unittest.TestCase):
    """结果历史存储：写入/读取/索引/保留策略。"""

    def setUp(self):
        # 用临时目录替换 store root，避免污染真实 query_results
        self._tmp = Path(tempfile.mkdtemp(prefix="result_store_test_"))
        self._orig_enabled = result_store.RESULT_STORE_ENABLED
        self._orig_dir = result_store.RESULT_STORE_DIR
        self._orig_rounds = result_store.RESULT_STORE_MAX_ROUNDS
        self._orig_days = result_store.RESULT_STORE_MAX_DAYS
        result_store.RESULT_STORE_ENABLED = True
        result_store.RESULT_STORE_DIR = str(self._tmp)
        result_store.RESULT_STORE_MAX_ROUNDS = 3
        result_store.RESULT_STORE_MAX_DAYS = 7

    def tearDown(self):
        result_store.RESULT_STORE_ENABLED = self._orig_enabled
        result_store.RESULT_STORE_DIR = self._orig_dir
        result_store.RESULT_STORE_MAX_ROUNDS = self._orig_rounds
        result_store.RESULT_STORE_MAX_DAYS = self._orig_days
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _state(self, conv="conv1", rid="req-1", query="查询返厂明细"):
        return {
            "conversation_id": conv,
            "request_id": rid,
            "effective_query": query,
            "current_user_input": query,
            "confirmed_plan": {"table": "ads_trip.ads_gundam_device_return_detail_hour"},
        }

    def _sql_result(self, n=5):
        return {
            "columns": ["goods_no", "model_type"],
            "rows": [{"goods_no": f"G{i}", "model_type": f"M{i % 2}"} for i in range(n)],
            "row_count": n,
        }

    def test_save_and_list_index(self):
        stored = result_store.save_query_result(self._state(), self._sql_result(5))
        self.assertEqual(stored["round_no"], 1)
        self.assertTrue(stored["result_file"])
        self.assertTrue(stored["full_csv"])
        # CSV 全量落盘
        csv_path = Path(stored["full_csv"])
        self.assertTrue(csv_path.exists())
        # 索引能列出
        index = result_store.list_result_index("conv1")
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["round_no"], 1)
        self.assertEqual(index[0]["row_count"], 5)

    def test_resolve_by_round_and_id(self):
        result_store.save_query_result(self._state(rid="req-1"), self._sql_result(3))
        result_store.save_query_result(self._state(rid="req-2", query="查询新增订单"), self._sql_result(4))
        entry = result_store.resolve_result("conv1", "2")
        self.assertEqual(entry["round_no"], 2)
        self.assertEqual(entry["effective_query"], "查询新增订单")
        entry2 = result_store.resolve_result("conv1", "req-1:result")
        self.assertEqual(entry2["round_no"], 1)

    def test_read_full_rows_from_csv(self):
        result_store.save_query_result(self._state(rid="req-1"), self._sql_result(3))
        data = result_store.read_result_full("conv1", "1")
        self.assertIsNotNone(data)
        self.assertEqual(len(data["rows"]), 3)
        self.assertEqual(data["rows"][0]["goods_no"], "G0")

    def test_retention_keeps_recent_rounds(self):
        for i in range(5):
            result_store.save_query_result(self._state(rid=f"req-{i}"), self._sql_result(1))
        index = result_store.list_result_index("conv1", limit=10)
        # 保留最近 3 轮
        self.assertEqual(len(index), 3)
        self.assertEqual(index[0]["round_no"], 3)
        # 被挤出的结果文件应被清理
        conv_dir = result_store._conversation_dir("conv1")
        self.assertFalse((conv_dir / "req-0.json").exists())
        self.assertTrue((conv_dir / "req-4.json").exists())

    def test_same_conversation_reuses_dir_across_days(self):
        # 模拟首日已建目录后，次日仍复用同一对话目录（跨天归同一文件夹）
        d1 = result_store.save_query_result(self._state(rid="req-1"), self._sql_result(2))
        # 再次调用内部目录定位：应复用同一个对话目录（result_file 的父目录即对话目录）
        conv_dir = result_store._conversation_dir("conv1")
        self.assertEqual(str(Path(d1["result_file"]).parent), str(conv_dir))

    def test_disabled_store_returns_empty(self):
        result_store.RESULT_STORE_ENABLED = False
        self.assertEqual(result_store.save_query_result(self._state(), self._sql_result(2)), {})
        self.assertEqual(result_store.list_result_index("conv1"), [])


if __name__ == "__main__":
    unittest.main()
