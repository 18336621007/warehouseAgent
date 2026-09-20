# thinking_stream_chat.py —— Planner 思考流式 ChatModel
# 底层用 OpenAI 兼容 SDK 流式调用：捕获 qwen thinking 的 reasoning_content 推送到前端思考面板（stream_bus），
# 输出保持 langchain AIMessage（content + tool_calls），兼容 with_structured_output / bind_tools。
import json
import os

from openai import OpenAI
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.output_parsers.pydantic import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool, is_basemodel_subclass
from pydantic import Field, PrivateAttr

from agentTest.config.settings import (
    get_llm_structured_output_method,
    get_llm_thinking_force_tool,
    get_llm_stream_reasoning,
    get_stream_output_enabled,
)
from agentTest.langgraph_app.runtime.stream_bus import get_stream_bus


def _lc_msg_to_oai(message: BaseMessage) -> dict:
    """把 langchain 消息转成 OpenAI chat messages 格式（含工具调用/工具结果）。"""
    msg_type = getattr(message, "type", "")
    if msg_type == "system":
        return {"role": "system", "content": str(message.content or "")}
    if msg_type == "human":
        return {"role": "user", "content": str(message.content or "")}
    if msg_type == "tool":
        return {
            "role": "tool",
            "content": str(message.content or ""),
            "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
        }
    # AI 消息：content + tool_calls（langchain 的 args 是 dict，需序列化为 JSON 字符串）
    oai_msg = {"role": "assistant", "content": str(message.content or "")}
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        oai_tool_calls = []
        for tc in tool_calls:
            fn_args = tc.get("args") or {}
            oai_tool_calls.append({
                "id": str(tc.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(tc.get("name") or ""),
                    "arguments": json.dumps(fn_args, ensure_ascii=False),
                },
            })
        oai_msg["tool_calls"] = oai_tool_calls
    return oai_msg


class _JsonFieldStreamer:
    """从 JSON 文本流中增量提取指定字符串字段的值（用于最终回答实时流式输出）。

    模型以流式生成 PlannerOutput JSON 时，respond_text 字段的文本会在生成过程中
    实时提取并推送前端，实现"边生成边输出"而非生成完再重放。
    """

    _ESC_MAP = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}

    def __init__(self, field: str):
        self._field = field
        self._stack = []        # 容器栈：'obj' / 'arr'
        self._state = "start"   # start/obj/key/colon/value/str/esc/uni/arr/arr_str/end
        self._key = ""
        self._in_target = False
        self._esc = ""

    def feed(self, text: str) -> str:
        """输入 JSON 增量片段，返回本段内可安全输出的目标字段文本。"""
        out = []
        for ch in text:
            self._step(ch, out)
        return "".join(out)

    def _container(self):
        # 栈顶容器对应的等待状态：对象等 key，数组等元素
        return "obj" if self._stack[-1] == "obj" else "arr"

    def _close_container(self):
        # 弹出当前容器并返回外层等待状态（栈空则结束）
        self._stack.pop()
        return "end" if not self._stack else self._container()

    def _step(self, ch, out):
        st = self._state
        if st == "start":
            if ch == "{":
                self._stack.append("obj")
                self._state = "obj"
        elif st == "obj":
            if ch == '"':
                self._key = ""
                self._state = "key"
            elif ch == "}":
                self._state = self._close_container()
        elif st == "key":
            if ch == "\\":
                self._state = "key_esc"
            elif ch == '"':
                self._state = "colon"
            else:
                self._key += ch
        elif st == "key_esc":
            self._key += ch
            self._state = "key"
        elif st == "colon":
            if ch == ":":
                self._state = "value"
        elif st == "value":
            if ch == '"':
                self._in_target = (len(self._stack) == 1 and self._stack[-1] == "obj" and self._key == self._field)
                self._state = "str"
            elif ch == "{":
                self._stack.append("obj")
                self._state = "obj"
            elif ch == "[":
                self._stack.append("arr")
                self._state = "arr"
            elif ch == ",":
                self._state = self._container()
            elif ch in "}]":
                self._state = self._close_container()
        elif st == "str":
            if ch == "\\":
                self._state = "esc"
                self._esc = ""
            elif ch == '"':
                self._in_target = False
                self._state = "end"
            elif self._in_target:
                out.append(ch)
        elif st == "esc":
            if ch == "u":
                self._state = "uni"
                self._esc = ""
            else:
                if self._in_target:
                    out.append(self._ESC_MAP.get(ch, "\\" + ch))
                self._state = "str"
        elif st == "uni":
            self._esc += ch
            if len(self._esc) == 4:
                if self._in_target:
                    try:
                        out.append(chr(int(self._esc, 16)))
                    except Exception:
                        out.append("?")
                self._state = "str"
        elif st == "arr":
            if ch == '"':
                self._state = "arr_str"
            elif ch == "{":
                self._stack.append("obj")
                self._state = "obj"
            elif ch == "[":
                self._stack.append("arr")
                self._state = "arr"
            elif ch == "]":
                self._state = self._close_container()
            # 数组元素间逗号/字面量忽略
        elif st == "arr_str":
            if ch == "\\":
                self._state = "arr_str_esc"
            elif ch == '"':
                self._state = "arr"
        elif st == "arr_str_esc":
            self._state = "arr_str"
        elif st == "end":
            if ch == ",":
                self._state = self._container()
            elif ch in "}]":
                self._state = self._close_container()


class ThinkingStreamChatModel(BaseChatModel):
    """思考流式 ChatModel：流式捕获 reasoning_content 推送前端，返回标准 AIMessage。"""
    model: str = Field(default="")
    extra_body: dict | None = Field(default=None)
    answer_field: str = Field(default="", description="最终回答流式：结构化 JSON 中该字段的文本在生成中实时推前端")
    stream_raw_content: bool = Field(default=False, description="自由文本流式：直接增量推送 content 到前端回答区（对齐 Codex）")
    _client: OpenAI = PrivateAttr()

    def __init__(self, api_key: str, base_url: str, model: str, extra_body=None, callbacks=None, answer_field="", stream_raw_content=False):
        super().__init__(model=model, extra_body=extra_body or None, callbacks=callbacks,
                         answer_field=answer_field, stream_raw_content=stream_raw_content)
        # OpenAI 客户端实例不作为 pydantic 字段，仅保存为私有属性
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    @property
    def _llm_type(self) -> str:
        return "thinking-stream-qwen"

    def with_structured_output(self, schema, *, include_raw=False, **kwargs):
        # 结构化输出方式由配置 LLM_STRUCTURED_OUTPUT_METHOD 驱动（json_mode/json_schema/function_calling）：
        # qwen thinking 模式不支持 tool_choice=required/object，用 response_format 类方式更通用；
        # langchain-core 基类只实现 function_calling，这里补 json_mode/json_schema 支持
        method = str(kwargs.pop("method", "") or "").strip().lower() or get_llm_structured_output_method()
        kwargs.pop("strict", None)
        if method == "json_mode":
            llm = self.bind(response_format={"type": "json_object"})
        elif method == "json_schema":
            tool = convert_to_openai_tool(schema)
            llm = self.bind(response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": tool["function"]["name"],
                    "schema": tool["function"]["parameters"],
                },
            })
        else:
            return super().with_structured_output(schema, include_raw=include_raw, **kwargs)
        if isinstance(schema, type) and is_basemodel_subclass(schema):
            output_parser = PydanticOutputParser(pydantic_object=schema)
        else:
            output_parser = JsonOutputParser()
        if include_raw:
            # include_raw 返回 raw/parsed/parsing_error 结构（与 langchain 一致）
            from langchain_core.runnables import RunnableMap, RunnablePassthrough
            from operator import itemgetter
            parser_assign = RunnablePassthrough.assign(
                parsed=itemgetter("raw") | output_parser, parsing_error=lambda _: None
            )
            parser_none = RunnablePassthrough.assign(parsed=lambda _: None)
            return RunnableMap(raw=llm) | parser_assign.with_fallbacks(
                [parser_none], exception_key="parsing_error"
            )
        return llm | output_parser

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """绑定工具：转 OpenAI 工具格式并透传 tool_choice。

        thinking 模式（qwen 等）不支持 tool_choice=required/object 时，按配置
        LLM_THINKING_FORCE_TOOL 决定是否强制工具调用；any/指定工具名按 ChatOpenAI 同规则归一。
        """
        formatted_tools = [convert_to_openai_tool(tool) for tool in tools]
        if tool_choice is not None:
            if isinstance(tool_choice, str):
                if tool_choice not in ("auto", "none", "required"):
                    if tool_choice == "any":
                        if get_llm_thinking_force_tool():
                            tool_choice = "required"
                        else:
                            # 不强制工具调用，避免 thinking 模式报错（模型自行决定是否调用）
                            tool_choice = None
                    else:
                        tool_choice = {"type": "function", "function": {"name": tool_choice}}
            elif isinstance(tool_choice, bool):
                tool_choice = "required" if get_llm_thinking_force_tool() else None
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        return super().bind(tools=formatted_tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 组装 OpenAI 兼容请求（stream 固定开启，思考内容逐字推前端）
        oai_messages = [_lc_msg_to_oai(m) for m in messages]
        request = {
            "model": self.model,
            "messages": oai_messages,
            "temperature": float(kwargs.get("temperature", 0)),
            "stream": True,
            # 流式请求在最后一个 chunk 返回 usage，用于 token 消耗统计（含缓存命中）
            "stream_options": {"include_usage": True},
        }
        if self.extra_body:
            request["extra_body"] = self.extra_body
        for key in ("tools", "tool_choice", "response_format", "max_tokens"):
            if kwargs.get(key) is not None:
                request[key] = kwargs[key]
        if stop:
            request["stop"] = stop

        bus = get_stream_bus()
        stream_reasoning = get_llm_stream_reasoning()
        reasoning_sid = f"reasoning-{os.urandom(3).hex()}"
        content_parts = []
        # 最终回答流式：从生成中的 JSON 增量提取指定字段（如 respond_text），实时推前端
        # 最终回答流式：自由文本直接推 content；否则从生成中的 JSON 增量提取 answer_field
        stream_raw = bool(self.stream_raw_content) and get_stream_output_enabled() and bus is not None
        stream_answer = (not stream_raw) and bool(self.answer_field) and get_stream_output_enabled() and bus is not None
        json_streamer = _JsonFieldStreamer(self.answer_field) if stream_answer else None
        # 流式工具调用增量按 index 累积（function calling / structured output）
        tool_calls_acc = {}
        usage = None
        response = self._client.chat.completions.create(**request)
        for chunk in response:
            if getattr(chunk, "usage", None) is not None:
                # 流式 usage 仅在末尾 chunk 返回（需 stream_options.include_usage），保存最后一个
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 思考内容（qwen thinking）：独立 stream_id 逐字推送到前端思考面板，不拼入正式输出
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning is None and hasattr(delta, "model_dump"):
                reasoning = delta.model_dump().get("reasoning_content")
            reasoning = reasoning or ""
            if reasoning and stream_reasoning and bus is not None:
                bus.emit_token("thinking", reasoning, stream_id=reasoning_sid)
            content = getattr(delta, "content", None) or ""
            if content:
                content_parts.append(content)
                if stream_raw:
                    # 自由文本：生成即推前端回答区（对齐 Codex 实时输出）
                    bus.emit_token("answer", content, live=True)
                elif json_streamer is not None:
                    # 已确认可输出的目标字段文本增量：实时推前端回答区
                    piece = json_streamer.feed(content)
                    if piece:
                        bus.emit_token("answer", piece, live=True)
            delta_tool_calls = getattr(delta, "tool_calls", None) or []
            for tc in delta_tool_calls:
                acc = tool_calls_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    acc["id"] = tc.id
                if tc.function and tc.function.name:
                    acc["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    acc["arguments"] += tc.function.arguments

        content = "".join(content_parts)
        llm_output = None
        if usage is not None:
            # 提取 token 用量（含缓存命中明细），供 llm.response 日志与请求级汇总
            try:
                usage_dump = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
            except Exception:
                usage_dump = {}
            llm_output = {"token_usage": usage_dump}
        ai_kwargs = {}
        if tool_calls_acc:
            tool_calls = []
            for idx in sorted(tool_calls_acc):
                acc = tool_calls_acc[idx]
                args_str = acc["arguments"] or "{}"
                try:
                    args = json.loads(args_str) if args_str.strip() else {}
                except Exception:
                    args = {}
                tool_calls.append({
                    "id": acc["id"] or f"call_{idx}",
                    "name": acc["name"],
                    "args": args,
                })
            ai_kwargs["tool_calls"] = tool_calls
        message = AIMessage(content=content or "", **ai_kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output=llm_output)
