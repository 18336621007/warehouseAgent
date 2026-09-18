# -*- coding: utf-8 -*-
# Planner LLM 调用瞬时错误重试测试：模型端 response_format JSON 偶发异常（APIError 400/5xx）时退避重试。
# 覆盖：APIError 重试后成功返回；持续 APIError 重试耗尽后上抛；非 APIError 异常直接上抛不重试。
import unittest
from unittest import mock

from openai import APIError

from agentTest.config.planner import MAX_LLM_RETRY
from agentTest.langgraph_app.nodes.planner_node import _invoke_llm_with_retry


class _FakeLLM:
    """模拟可 invoke 的 LLM：按脚本依次抛异常或返回结果。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class PlannerLlmRetryTest(unittest.TestCase):
    """Planner LLM 瞬时错误重试行为。"""

    def _patch_sleep(self):
        # 跳过退避等待，避免测试慢
        return mock.patch("agentTest.langgraph_app.nodes.planner_node.time.sleep")

    def test_retry_after_api_error_then_success(self):
        # 第一次抛 APIError，第二次成功 → 返回第二次结果，共调用 2 次
        llm = _FakeLLM([APIError("400 InternalError.Algo.InvalidParameter", request=mock.Mock(), body=None), "ok"])
        with self._patch_sleep():
            result = _invoke_llm_with_retry(llm, ["msg"])
        self.assertEqual(result, "ok")
        self.assertEqual(llm.calls, 2)

    def test_retry_exhausted_raises_last_error(self):
        # 持续抛 APIError → 重试 MAX_LLM_RETRY 次后上抛最后一个异常，共调用 MAX_LLM_RETRY+1 次
        llm = _FakeLLM([APIError("bad", request=mock.Mock(), body=None)] * (MAX_LLM_RETRY + 1))
        with self._patch_sleep():
            with self.assertRaises(APIError):
                _invoke_llm_with_retry(llm, ["msg"])
        self.assertEqual(llm.calls, MAX_LLM_RETRY + 1)

    def test_non_api_error_not_retried(self):
        # 非 APIError 异常（如 ValueError）直接上抛，不重试
        llm = _FakeLLM([ValueError("boom")])
        with self.assertRaises(ValueError):
            _invoke_llm_with_retry(llm, ["msg"])
        self.assertEqual(llm.calls, 1)

    def test_first_try_success_no_extra_call(self):
        # 首次即成功：只调用 1 次
        llm = _FakeLLM(["ok"])
        result = _invoke_llm_with_retry(llm, ["msg"])
        self.assertEqual(result, "ok")
        self.assertEqual(llm.calls, 1)


if __name__ == "__main__":
    unittest.main()
