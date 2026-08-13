# -*- coding: utf-8 -*-
# 简要注释：LangChain LLM 调用日志回调，统一记录 llm.call / llm.response / llm.error。
import json
import threading
from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler

from agentTest.config import log_config
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_llm_call
from agentTest.langgraph_app.runtime.graph_logger import log_llm_error
from agentTest.langgraph_app.runtime.graph_logger import log_llm_response


class GraphLLMHandler(BaseCallbackHandler):
    """按请求上下文记录 LLM 调用的回调：调用方通过 caller 或 metadata.caller 标记。"""

    def __init__(self, caller="llm"):
        self.caller = caller
        # 并发请求可能共享同一个 handler，用线程本地存储隔离 run 状态；
        # 属性必须在线程内惰性初始化，LangGraph 节点可能在其他线程执行
        self._local = threading.local()

    def _starts(self):
        # 当前线程的 run 状态字典，首次访问时创建，避免跨线程 AttributeError
        starts = getattr(self._local, "starts", None)
        if starts is None:
            starts = {}
            self._local.starts = starts
        return starts

    @staticmethod
    def _effective_caller(default_caller, metadata=None):
        # 允许通过 invoke config 的 metadata.caller 覆盖默认调用方（如 reranker 复用 Advisor llm）
        return (metadata or {}).get("caller") or default_caller

    @staticmethod
    def _extract_input(messages):
        # 提取除系统提示词外的所有消息（human/ai/tool），
        # 保留 Agent 循环每次调用的真实输入差异（工具调用与工具结果）
        lines = []
        for msg_list in messages or []:
            for msg in msg_list or []:
                if getattr(msg, "type", "") == "system":
                    continue
                msg_type = getattr(msg, "type", "message")
                content = getattr(msg, "content", "")
                if isinstance(content, (list, dict)):
                    content = json.dumps(content, ensure_ascii=False)
                tool_calls = getattr(msg, "tool_calls", None) or []
                parts = []
                if content:
                    parts.append(str(content))
                if tool_calls:
                    # AI 消息 content 通常为空，工具调用信息在 tool_calls 中
                    for tc in tool_calls:
                        name = tc.get("name", "?")
                        args = tc.get("args") or {}
                        parts.append(
                            f"调用工具: {name}({json.dumps(args, ensure_ascii=False)})"
                        )
                if parts:
                    lines.append(f"[{msg_type}] " + " ".join(parts))
        return lines

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        # chat model 统一走这里：记录用户输入（不含系统提示词）并完成调用计数
        if not log_config.LOG_LLM_ENABLED:
            return
        model = (serialized or {}).get("kwargs", {}).get("model") or ""
        caller = self._effective_caller(self.caller, metadata)
        user_lines = self._extract_input(messages)
        self._starts()[run_id] = {
            "timer": perf_counter(),
            "prompts": user_lines,
            "model": model,
            "caller": caller,
        }
        # 立即写 llm.call：只记录用户输入，供 trace 回放与请求级 LLM 计数
        log_llm_call(caller, model, user_lines, call_id=run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **kwargs):
        if not log_config.LOG_LLM_ENABLED:
            return
        # 已由 on_chat_model_start 处理（chat model），避免重复记录
        if run_id in self._starts():
            return
        model = (serialized or {}).get("kwargs", {}).get("model") or ""
        caller = self._effective_caller(self.caller, metadata)
        self._starts()[run_id] = {
            "timer": perf_counter(),
            "prompts": list(prompts or []),
            "model": model,
            "caller": caller,
        }
        # 兜底：非 chat model 直接记录原始 prompt
        log_llm_call(caller, model, list(prompts or []), call_id=run_id)

    def on_llm_end(self, response, *, run_id, **kwargs):
        if not log_config.LOG_LLM_ENABLED:
            return
        start = self._starts().pop(run_id, None)
        if start is None:
            return
        output = ""
        for generation_list in (response.generations or []):
            for generation in generation_list:
                output += generation.text or ""
        llm_output = response.llm_output or {}
        token_usage = llm_output.get("token_usage") or {}
        log_llm_response(
            start["caller"],
            start["model"],
            output,
            ms=elapsed_ms(start["timer"]),
            tokens={
                "prompt_tokens": token_usage.get("prompt_tokens"),
                "completion_tokens": token_usage.get("completion_tokens"),
                "total_tokens": token_usage.get("total_tokens"),
            },
            call_id=run_id,
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        if not log_config.LOG_LLM_ENABLED:
            return
        start = self._starts().pop(run_id, None)
        if start is None:
            return
        log_llm_error(
            start["caller"],
            start["model"],
            str(error),
            ms=elapsed_ms(start["timer"]),
            call_id=run_id,
        )


def build_llm_logging_handler(caller="llm"):
    """创建绑定到指定调用方的 LLM 日志回调，供各 Agent 的 ChatOpenAI 挂载。"""
    return GraphLLMHandler(caller=caller)
