# -*- coding: utf-8 -*-
# _JsonFieldStreamer 测试：验证从流式 JSON 中增量提取 respond_text 的准确性。
import json
import unittest

from agentTest.langgraph_app.services.thinking_stream_chat import _JsonFieldStreamer


class JsonFieldStreamerTest(unittest.TestCase):
    """最终回答流式提取：JSON 文本流中增量提取指定字段。"""

    def _extract(self, data, field="respond_text", chunk=1):
        text = json.dumps(data, ensure_ascii=False)
        streamer = _JsonFieldStreamer(field)
        out = []
        for i in range(0, len(text), chunk):
            out.append(streamer.feed(text[i:i + chunk]))
        return "".join(out)

    def test_extract_respond_text_with_escapes(self):
        # 含换行/引号/反斜杠/unicode 的 respond_text，逐字符 feed 应完整还原
        data = {
            "effective_query": "查询返厂明细",
            "route": "respond",
            "tables": ["ads_trip.ads_gundam_device_return_detail_hour"],
            "fields": ["region_name", "status"],
            "respond_text": "## 结果\n共 26 条，含 \"引号\" 与 \\反斜杠\\ 及 emoji \u4f60\u597d",
            "reason": "已查询",
        }
        self.assertEqual(self._extract(data), data["respond_text"])

    def test_extract_skip_nested_arrays_objects(self):
        # respond_text 前有嵌套数组/对象字段，应正确跳过并提取
        data = {
            "effective_query": "q",
            "route": "respond",
            "tables": [{"a": 1}, ["x", "y"], "z"],
            "semantic_metrics": [{"id": "m1", "confidence": 0.9}],
            "respond_text": "最终回答内容",
            "reason": "r",
        }
        self.assertEqual(self._extract(data), "最终回答内容")

    def test_chunked_feed_equals_single(self):
        # 分块 feed（模拟真实流式 chunk 大小不一）应与一次性提取一致
        data = {"respond_text": "第一行\n第二行，含\"引号\"和\\斜杠\\", "x": [1, 2, 3]}
        full = "".join([
            _JsonFieldStreamer("respond_text").feed(data)
            for data in []  # noqa
        ])
        # 用同一个 streamer 分块
        streamer = _JsonFieldStreamer("respond_text")
        out = []
        text = json.dumps(data, ensure_ascii=False)
        for i in range(0, len(text), 3):
            out.append(streamer.feed(text[i:i + 3]))
        self.assertEqual("".join(out), data["respond_text"])

    def test_no_target_field_yields_empty(self):
        # 目标字段缺失或非 JSON：不应有输出
        streamer = _JsonFieldStreamer("respond_text")
        out = []
        for ch in json.dumps({"route": "respond", "reason": "x"}):
            out.append(streamer.feed(ch))
        self.assertEqual("".join(out), "")
        streamer2 = _JsonFieldStreamer("respond_text")
        self.assertEqual(streamer2.feed("这不是 JSON 文本"), "")

    def test_multiple_occurrences_only_first(self):
        # 只提取第一个匹配字段（顶层对象单字段场景）
        data = {"respond_text": "回答A", "other": {"respond_text": "回答B"}}
        self.assertEqual(self._extract(data), "回答A")


# -*- coding: utf-8 -*-
# 追加：验证 _generate 在生成 JSON 过程中实时 emit respond_text（live=true 流式）。
import json as _json
from unittest import mock

from langchain_core.messages import HumanMessage
from agentTest.langgraph_app.services.thinking_stream_chat import ThinkingStreamChatModel


class _Delta:
    def __init__(self, content="", reasoning=""):
        self.content = content
        self.reasoning_content = reasoning


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, content="", reasoning=""):
        self.choices = [_Choice(_Delta(content, reasoning))]


class _FakeBus:
    def __init__(self):
        self.events = []

    def emit_token(self, scope, text, stream_id="", live=True):
        self.events.append({"scope": scope, "text": text, "live": live, "stream_id": stream_id})

    def emit(self, event):
        pass


def _stream_generate_answer_test():
    data = {"effective_query": "q", "route": "respond",
            "respond_text": "## 结果\n共 26 条，含 \"引号\" 与 \\反斜杠\\", "reason": "r"}
    json_text = _json.dumps(data, ensure_ascii=False)
    chunks = [_Chunk(c) for c in json_text]  # 逐字符模拟流式生成

    model = ThinkingStreamChatModel(api_key="k", base_url="http://x", model="m",
                                    answer_field="respond_text")
    bus = _FakeBus()
    with mock.patch.object(model._client.chat.completions, "create", return_value=iter(chunks)), \
         mock.patch("agentTest.langgraph_app.services.thinking_stream_chat.get_stream_bus", return_value=bus), \
         mock.patch("agentTest.langgraph_app.services.thinking_stream_chat.get_llm_stream_reasoning", return_value=True), \
         mock.patch("agentTest.langgraph_app.services.thinking_stream_chat.get_stream_output_enabled", return_value=True):
        model._generate([HumanMessage(content="hi")])
    answer_events = [e for e in bus.events if e["scope"] == "answer"]
    return "".join(e["text"] for e in answer_events), answer_events


class ThinkingStreamEmitTest(unittest.TestCase):
    """验证最终回答在模型生成 JSON 过程中实时流式 emit（live=true），而非生成完再重放。"""

    def test_respond_text_emitted_live_during_generation(self):
        joined, answer_events = _stream_generate_answer_test()
        self.assertEqual(joined, "## 结果\n共 26 条，含 \"引号\" 与 \\反斜杠\\")
        self.assertTrue(answer_events, "应实时 emit answer token")
        self.assertTrue(all(e["live"] for e in answer_events), "应为 live=true 实时流")
        self.assertGreater(len(answer_events), 1, "应分多段 emit，而非整段一次")



if __name__ == "__main__":
    unittest.main()
