# result_query_tool 受控中间结果查询工具单元测试
# 覆盖：view / group_by_count / group_by_sum / filter_count、非法 ref、非法字段、limit 上限
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agentTest.langgraph_app.services import result_store
from agentTest.langgraph_app.tools.result_query_tool import (
    build_result_query_tool,
    set_result_conversation,
    reset_result_conversation,
    MAX_RESULT_QUERY_LIMIT,
)


class ResultQueryToolTest(unittest.TestCase):
    """基于落盘结果的受控查询：确定性计算 + 字段白名单。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="result_query_tool_test_"))
        self._orig_enabled = result_store.RESULT_STORE_ENABLED
        self._orig_dir = result_store.RESULT_STORE_DIR
        result_store.RESULT_STORE_ENABLED = True
        result_store.RESULT_STORE_DIR = str(self._tmp)
        self.tool = build_result_query_tool()
        self._conv_token = set_result_conversation("conv1")

    def tearDown(self):
        reset_result_conversation(self._conv_token)
        result_store.RESULT_STORE_ENABLED = self._orig_enabled
        result_store.RESULT_STORE_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _state(self, rid="req-1"):
        return {
            "conversation_id": "conv1",
            "request_id": rid,
            "effective_query": "查询返厂明细",
            "current_user_input": "查询返厂明细",
            "confirmed_plan": {"table": "ads_trip.ads_gundam_device_return_detail_hour"},
        }

    def _sql_result(self):
        rows = [
            {"goods_no": "G1", "disable_type": "批量召回", "model_type": "M1", "amount": 10},
            {"goods_no": "G2", "disable_type": "批量召回", "model_type": "M2", "amount": 20},
            {"goods_no": "G3", "disable_type": "多次诊断无故障", "model_type": "M1", "amount": 5},
            {"goods_no": "G4", "disable_type": "多次诊断无故障", "model_type": "M1", "amount": 15},
        ]
        return {
            "columns": ["goods_no", "disable_type", "model_type", "amount"],
            "rows": rows,
            "row_count": len(rows),
        }

    def _save(self):
        result_store.save_query_result(self._state(), self._sql_result())

    def test_view_returns_preview(self):
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "view", "limit": 2})
        self.assertIn("批量召回", out)
        self.assertIn("仅展示前 2 行", out)
        self.assertIn("全量数据文件", out)

    def test_group_by_count(self):
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "group_by_count", "group_by": ["disable_type"]})
        self.assertIn("按 disable_type 分组计数", out)
        self.assertIn("批量召回: 2", out)
        self.assertIn("多次诊断无故障: 2", out)

    def test_group_by_count_with_json_string(self):
        # 兼容 LLM 把 list 参数序列化成 JSON 数组字符串
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "group_by_count", "group_by": "[\"disable_type\"]"})
        self.assertIn("按 disable_type 分组计数", out)
        self.assertIn("批量召回: 2", out)
        self.assertIn("多次诊断无故障: 2", out)

    def test_group_by_count_with_plain_string(self):
        # 兼容单字段字符串
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "group_by_count", "group_by": "disable_type"})
        self.assertIn("按 disable_type 分组计数", out)
        self.assertIn("批量召回: 2", out)

    def test_group_by_count_with_comma_string(self):
        # 兼容逗号分隔的多字段字符串
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "group_by_count", "group_by": "model_type, disable_type"})
        self.assertIn("按 model_type / disable_type 分组计数", out)

    def test_group_by_sum(self):
        self._save()
        out = self.tool.invoke({
            "ref": "1",
            "operation": "group_by_sum",
            "group_by": ["model_type"],
            "agg_field": "amount",
        })
        self.assertIn("按 model_type 分组对 amount 求和", out)
        self.assertIn("M1: 30", out)
        self.assertIn("M2: 20", out)

    def test_filter_count(self):
        self._save()
        out = self.tool.invoke({
            "ref": "1",
            "operation": "filter_count",
            "filters": "disable_type='批量召回'",
        })
        self.assertIn("筛选后共 2 行", out)

    def test_invalid_ref(self):
        out = self.tool.invoke({"ref": "99", "operation": "view"})
        self.assertIn("无法定位第 99 轮结果", out)

    def test_missing_ref(self):
        out = self.tool.invoke({"operation": "view"})
        self.assertIn("缺少 ref 参数", out)

    def test_invalid_field_rejected(self):
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "group_by_count", "group_by": ["不存在的列"]})
        self.assertIn("不在第 1 轮结果列中", out)

    def test_invalid_agg_field_rejected(self):
        self._save()
        out = self.tool.invoke({
            "ref": "1", "operation": "group_by_sum", "group_by": ["disable_type"], "agg_field": "不存在的列",
        })
        self.assertIn("不在第 1 轮结果列中", out)

    def test_invalid_filter_field_rejected(self):
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "filter_count", "filters": "not_a_column=1"})
        self.assertIn("过滤字段 not_a_column 不在第 1 轮结果列中", out)

    def test_limit_capped(self):
        self._save()
        out = self.tool.invoke({"ref": "1", "operation": "view", "limit": 99999})
        self.assertNotIn("仅展示前 99999 行", out)


if __name__ == "__main__":
    unittest.main()
