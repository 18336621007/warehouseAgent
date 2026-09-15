# -*- coding: utf-8 -*-
# 请求级 LLM token 聚合器单测：add/get/clear 累加与 request_id 隔离
import unittest

from agentTest.langgraph_app.runtime.graph_logger import (
    add_llm_usage,
    bind_log_context,
    clear_llm_token_usage,
    get_llm_token_usage,
    reset_log_context,
)


class TestLlmUsageTracker(unittest.TestCase):
    """token 聚合器基础能力（跨线程按 request_id 累加）。"""

    def test_add_get_clear(self):
        token = bind_log_context(
            conversation_id="conv_t",
            topic_id="topic_t",
            request_id="req_t",
            graph_thread_id="thread_t",
        )
        try:
            self.assertEqual(get_llm_token_usage(), {})
            add_llm_usage(input_tokens=100, output_tokens=20, cache_hit=30, cache_miss=70)
            add_llm_usage(input_tokens=50, output_tokens=10, cache_hit=50, cache_miss=0)
            usage = get_llm_token_usage()
            self.assertEqual(usage["input_tokens"], 150)
            self.assertEqual(usage["output_tokens"], 30)
            self.assertEqual(usage["cache_hit"], 80)
            self.assertEqual(usage["cache_miss"], 70)
            clear_llm_token_usage()
            self.assertEqual(get_llm_token_usage(), {})
        finally:
            reset_log_context(token)

    def test_request_isolation(self):
        token = bind_log_context(
            conversation_id="conv_a",
            topic_id="topic_a",
            request_id="req_a",
            graph_thread_id="thread_a",
        )
        try:
            add_llm_usage(input_tokens=10, output_tokens=5, cache_hit=0, cache_miss=10)
        finally:
            reset_log_context(token)
        token2 = bind_log_context(
            conversation_id="conv_b",
            topic_id="topic_b",
            request_id="req_b",
            graph_thread_id="thread_b",
        )
        try:
            # 不同 request 之间互不干扰
            self.assertEqual(get_llm_token_usage(), {})
        finally:
            reset_log_context(token2)
            clear_llm_token_usage()


if __name__ == "__main__":
    unittest.main()
