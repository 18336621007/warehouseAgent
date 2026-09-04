# Advisor 子图：口径澄清 + 共享草稿维护 + 字段核验
# Planner 是唯一路由者：Advisor 只负责和用户澄清、用工具核验表/字段真实性、
# 通过 update_draft_plan 把已确认槽位写入共享草稿（status=draft）。
# 不再锁定方案、不再请求用户最终确认，是否进入 Seeker 由 Planner 统一判定。
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.state.advisor_state import AdvisorState
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body, get_stream_output_enabled
from agentTest.langgraph_app.runtime.stream_bus import get_stream_bus
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms, log_node_event
from agentTest.langgraph_app.runtime.graph_logger import log_tools_called, log_advisor_mode
from agentTest.langgraph_app.runtime.graph_logger import log_state_snapshot
from agentTest.langgraph_app.runtime.graph_logger import log_metric_event
from agentTest.langgraph_app.runtime.llm_log_handler import build_llm_logging_handler
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from agentTest.config.advisor import (
    MAX_COLUMN_CHECK_RETRIES,
)
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.callbacks import BaseCallbackHandler
from agentTest.langgraph_app.services.query_plan_service import (
    merge_draft_plan,
)
from agentTest.semantic_layer.metric_matcher import (
    match_metrics_from_query,
    format_metric_context,
)



class _AdvisorThinkingCollector(BaseCallbackHandler):
    """收集 Advisor ReAct 每步 LLM 输出（文本 + 工具调用），供前端思考过程展示。"""

    def __init__(self):
        self.lines: list[str] = []

    def on_llm_end(self, response, **kwargs):
        # 提取本次 LLM 输出的文本；若输出的是工具调用，则转成可读的“调用工具: ...”
        text = ""
        tool_lines = []
        for generation_list in (response.generations or []):
            for generation in generation_list:
                text += generation.text or ""
                message = getattr(generation, "message", None)
                if message is not None:
                    for tc in (getattr(message, "tool_calls", None) or []):
                        # 工具调用只显示工具名，不输出完整参数，避免思考过程过长
                        tool_lines.append("调用工具: " + tc.get("name", "?"))
        if tool_lines:
            text += "\n" + "\n".join(tool_lines)
        if text.strip():
            self.lines.append(text.strip())
            # 工具名在调用结束时才完整，无法逐字，随思考流实时追加一行（带 run_id 方便前端归属段落）
            bus = get_stream_bus()
            if bus is not None:
                bus.emit_token(
                    "thinking",
                    "\n" + "\n".join(tool_lines),
                    stream_id=str(kwargs.get("run_id", "")),
                )


class _AdvisorTokenHandler(BaseCallbackHandler):
    """把 Advisor 每步 LLM 输出的 token 实时转发给前端思考过程。
    无工具调用的输出是最终回复：从思考面板移除，并在回答区逐字重放。
    该 handler 挂在全局复用的 LLM 上，状态放在请求级的 StreamBus 上。"""

    def __init__(self):
        # run_id -> 本次 LLM 调用的 token 缓冲，供最终回复判定后重放
        self._buffers: dict = {}

    def on_llm_new_token(self, token: str, **kwargs):
        bus = get_stream_bus()
        if bus is None or not token:
            return
        run_id = str(kwargs.get("run_id", ""))
        if not bus.advisor_label_sent:
            bus.advisor_label_sent = True
            # 首个 token 前先发节点标签，让“正在核验...”提示出现在内容之前
            bus.emit({"type": "thinking", "node": "advisor_agent", "text": "正在核验元数据并确认口径..."})
        # 实时推送思考（保持逐字），同时缓冲供最终回复判定
        bus.emit_token("thinking", token, stream_id=run_id)
        self._buffers.setdefault(run_id, []).append(token)

    def on_llm_end(self, response, **kwargs):
        run_id = str(kwargs.get("run_id", ""))
        tokens = self._buffers.pop(run_id, None)
        if not tokens:
            return
        # 判断本次输出是否携带工具调用：无工具调用即为最终回复
        has_tool_calls = False
        for generation_list in (response.generations or []):
            for generation in generation_list:
                message = getattr(generation, "message", None)
                if message is not None and getattr(message, "tool_calls", None):
                    has_tool_calls = True
        if has_tool_calls:
            return
        # 最终回复：思考面板移除该段，回答区逐字重放
        bus = get_stream_bus()


def _get_tool_call_args(messages, tool_name):
    """从一批消息里找出所有指定名称的工具调用，返回 (参数, 调用id) 列表。"""
    results = []
    for msg in messages:  # 遍历每条消息
        for tc in (getattr(msg, "tool_calls", None) or []):  # 取消息的 tool_calls（AI消息才有）
            if tc.get("name") == tool_name:  # 只留指定工具
                results.append((tc.get("args", {}), tc.get("id", "")))  # (参数, 调用id)
    return results


def _missing_column_search_tables(messages, tables: list[str]) -> list[str]:
    """返回尚未调用 search_columns 检索字段的目标表清单（空列表=全部已检索）。"""
    if not tables:
        return []
    search_args_list = _get_tool_call_args(
        messages,
        "search_columns",
    )
    searched = {
        args.get("table", "")
        for args, _ in search_args_list
        if args.get("table")
    }
    return [t for t in tables if t not in searched]


def _normalize_clarification_message(message: str) -> str:
    """兜底：LLM 未产出有效回复时的澄清提示。"""
    content = (message or "").strip()
    if not content:
        return "当前仍有查询口径需要确认，请补充最关键的指标、维度或过滤条件。"
    return content


def _extract_field_meaning(page_content: str, mark_alias: bool = False) -> str:
    """从字段元数据文本提取中文含义：优先原始备注，其次首个别名。"""
    for line in (page_content or "").splitlines():
        if line.startswith("原始备注:"):
            return line[len("原始备注:"):].strip()[:60]
    for line in (page_content or "").splitlines():
        if line.startswith("别名:"):
            aliases = line[len("别名:"):].strip()
            first = aliases.split("、")[0].strip()[:40]
            return f"{first}（推断别名）" if mark_alias else first
    return ""


def _extract_table_meaning(page_content: str) -> str:
    """从表元数据文本提取核心含义：优先备注，其次描述。"""
    for line in (page_content or "").splitlines():
        if line.startswith("表含义:"):
            return line[len("表含义:"):].strip()[:80]
    for line in (page_content or "").splitlines():
        if line.startswith("表描述:"):
            return line[len("表描述:"):].strip()[:80]
    return ""


def _extract_aliases_from_comment(page_content: str) -> str:
    """从字段元数据文本提取别名列表（用、分隔）。"""
    for line in (page_content or "").splitlines():
        if line.startswith("别名:"):
            return line[len("别名:"):].strip()
    return ""


def build_advisor_subgraph(runtime):
    """构建 Advisor ReAct Agent 子图 —— 纯澄清 + 草稿维护，不锁定方案。"""
    # 挂载 LLM 日志回调：记录 prompt/输出/耗时，便于 trace 回放
    llm = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,
        extra_body=get_model_extra_body(),
        streaming=get_stream_output_enabled(),
        callbacks=[
            build_llm_logging_handler("advisor"),
            _AdvisorTokenHandler(),
        ],
    )

    tools = build_advisor_tools(
        runtime["db_vector_store"],
        runtime["table_vector_store"],
        runtime["column_vector_store"],
        runtime.get("bm25_retriever"),
    )

    plan_agent = create_agent(
        llm,
        tools,
        system_prompt=ADVISOR_SYSTEM_PROMPT,
    )

    graph = StateGraph(AdvisorState)

    def run_advisor(state):
        """处理用户问题：澄清口径 + 校验表/字段 + 更新共享草稿。

        Planner 提供还原后的完整需求；Advisor 不锁定方案、不决定是否执行，
        是否进入 Seeker 由 Planner 在下一轮统一判定。"""
        # Planner 已将“1”“A”等短回答还原为完整有效需求
        planner_entities = state.get("planner_entities") or {}
        effective_query = (
                state.get("effective_query")
                or planner_entities.get("effective_query")
                or state.get("current_user_input", "")
        )
        current_user_input = state.get("current_user_input", "")
        question = effective_query
        timer = start_timer()
        log_node_start("advisor_agent", question=question[:60])

        # messages是当前Topic唯一的标准消息历史
        history = list(state.get("messages") or [])
        advisor_turns = state.get("advisor_turns", 0)

        # ── 语义层权威口径注入：供 Advisor 核验字段参考 ──
        # 优先复用 Planner 的 grep 候选（含 notes/definition 命中，避免漏召"调出"等），
        # 无候选时回退本节点词法匹配
        metric_search_text = effective_query or current_user_input
        _planner_semantic_candidates = list(
            planner_entities.get("semantic_candidates") or []
        )
        if _planner_semantic_candidates:
            semantic_matches = _planner_semantic_candidates
        else:
            semantic_matches = match_metrics_from_query(metric_search_text, limit=5)
        # 语义层命中日志：便于排查"走了语义层还是召回"
        log_metric_event(
            "semantic.match",
            node_name="advisor",
            mention=metric_search_text[:100],
            hit_count=len(semantic_matches),
            metric_ids=[m.get("id", "") for m in semantic_matches],
            metric_names=[m.get("name", "") for m in semantic_matches],
            metric_scores=[m.get("score", 0) for m in semantic_matches],
            metric_confidences=[
                round(float(m.get("confidence", 0) or 0), 2)
                for m in semantic_matches
            ],
            top_confidence=round(
                max(
                    (float(m.get("confidence", 0) or 0) for m in semantic_matches),
                    default=0.0,
                ),
                2,
            ),
        )
        metric_context_text = format_metric_context(semantic_matches)

        # 将 Planner 分析结果和当前旧方案作为结构化上下文提供给 Advisor
        planner_tables = planner_entities.get("tables") or []
        planner_fields = planner_entities.get("fields") or []
        planner_completeness = planner_entities.get("completeness", "")
        planner_reason = state.get("planner_reason", "")
        # 上次执行失败原因（如缺 join 契约），供 Advisor 调整方案或告知用户
        plan_error = planner_entities.get("plan_error") or state.get("seeker_plan_error") or ""

        agent = plan_agent
        advisor_mode = "clarify" if planner_completeness != "full" else "draft"
        log_advisor_mode(
            "advisor_agent",
            mode=advisor_mode,
            completeness=planner_completeness or "none",
        )

        # confirmed_plan 同时承载草稿（status=draft），Advisor 只读写草稿
        current_plan = state.get("confirmed_plan") or {}

        context_lines = [
            "【Planner还原的当前完整需求】",
            effective_query,
            "",
            "【用户本轮原始输入】",
            current_user_input,
        ]

        # 语义层权威口径插入在最前，紧接着 Planner 还原需求
        if metric_context_text:
            context_lines = [metric_context_text] + context_lines

        if planner_tables:
            context_lines.extend([
                "",
                "【Planner候选表】",
                ", ".join(planner_tables),
            ])

        if planner_fields:
            context_lines.extend([
                "",
                "【Planner已确定字段】",
                ", ".join(planner_fields),
            ])

        if planner_completeness:
            context_lines.extend([
                "",
                "【Planner模糊度】",
                planner_completeness,
            ])

        if planner_reason:
            context_lines.extend([
                "",
                "【Planner判断原因】",
                planner_reason,
            ])

        if plan_error:
            context_lines.extend([
                "",
                "【上次执行失败原因】",
                plan_error,
                "请据此调整方案：尝试改用不涉及缺失关系的表/字段，或向用户说明无法执行的原因。",
            ])

        context_lines.extend([
            "",
            "【本轮操作模式】",
            "你是口径澄清与核验助手：用工具核对真实表/字段，把已确认槽位写入草稿；"
            "需要用户补充信息或确认口径时，只问一个最关键的问题；"
            "用户询问口径区别或闲聊时，直接回答。不要锁定方案，是否执行由 Planner 决定。",
        ])

        # 有草稿则填入当前方案
        if current_plan:
            context_lines.extend([
                "",
                "【当前已有草稿方案】",
                f"状态：{current_plan.get('status', '未设置')}",
                f"数据表：{', '.join(current_plan.get('tables') or []) or '未设置'}",
                f"度量：{', '.join(current_plan.get('measures') or []) or '无'}",
                f"维度：{', '.join(current_plan.get('dimensions') or []) or '无'}",
                f"时间字段：{current_plan.get('time_field', '未设置')}",
                f"时间范围：{current_plan.get('time_range', '未设置')}",
                f"过滤条件：{current_plan.get('filters', '') or '无'}",
                "用户可能只修改了部分内容，未修改内容应尽可能保留；"
                "如果整体目标已经变化，可以重新构建完整草稿。",
            ])

        msg_content = "\n".join(context_lines)

        # 从检索结果中提取出表/字段原始备注，防止使用增强后的别名
        table_candidates = planner_entities.get("table_candidates") or []
        column_candidates = planner_entities.get("column_candidates") or []
        candidate_lines = []
        if table_candidates:
            candidate_lines.append("Planner 检索结果 - 候选表：")
            for tc in table_candidates:
                # 表描述只保留核心功能摘要，避免长文本截断
                table_desc = _extract_table_meaning(tc["comment"]) or tc["comment"][:100]
                candidate_lines.append(f"  {tc['table']} (相似度={tc['score']}) - {table_desc}")
        if column_candidates:
            candidate_lines.append("Planner 检索结果 - 候选字段：")
            for cc in column_candidates:
                # 检索阶段保留原始备注与别名；原始备注必须可见，避免 LLM 只看别名
                field_desc = _extract_field_meaning(cc["comment"]) or cc["field"]
                aliases = _extract_aliases_from_comment(cc["comment"])
                if aliases:
                    field_desc = f"{field_desc}；别名: {aliases}"
                candidate_lines.append(f"  {cc['table']}.{cc['field']} (相似度={cc['score']}) - {field_desc}")
        candidate_text = "\n".join(candidate_lines) if candidate_lines else ""

        # 构造传给Agent的完整消息列表，把Planner还原的上下文装进用户信息
        agent_history = list(history)
        # candidate_text 是 Planner 检索到的候选表/候选字段摘要，追加为固定参考上下文
        if candidate_text:
            agent_history.insert(0, SystemMessage(content=candidate_text))
        # 多轮场景：就地替换最后一条用户消息
        if agent_history and isinstance(agent_history[-1], HumanMessage):
            current_user_message = agent_history[-1]
            agent_history[-1] = HumanMessage(
                content=msg_content,  # 替换内容
                name=current_user_message.name,  # 保留 name="user"
                id=current_user_message.id,  # 保留原 id
            )
        else:  # 首轮/历史末尾不是用户消息：追加新 HumanMessage
            agent_history.append(HumanMessage(
                content=msg_content,
                name="user",
                id=f"{state['request_id']}:user",
            ))

        # 只持久化Agent调用后新增的消息
        persist_start_index = len(agent_history)

        new_history = None
        retries = 0
        # 收集 Advisor ReAct 每步 LLM 输出，供前端思考过程展示
        thinking_collector = _AdvisorThinkingCollector()

        # 写草稿前必须先检索目标表字段，保证字段真实（安全，不干预口径）
        while retries <= MAX_COLUMN_CHECK_RETRIES:
            result = agent.invoke(
                {"messages": agent_history},
                config={"callbacks": [thinking_collector]},
            )
            new_history = result["messages"]

            # 只检查本轮 update_draft_plan 的目标表是否检索过字段
            current_round_messages = new_history[persist_start_index:]
            draft_args_list = _get_tool_call_args(
                current_round_messages,
                "update_draft_plan",
            )
            if draft_args_list:
                draft_args, _ = draft_args_list[-1]
                proposed_tables = draft_args.get("tables") or []
                missing_tables = _missing_column_search_tables(
                    new_history,
                    proposed_tables,
                )
                if missing_tables:
                    retries += 1
                    log_node_event(
                        "advisor_agent",
                        "拦截 update_draft_plan："
                        f"目标表未检索字段 {missing_tables}，"
                        f"重试 {retries}/{MAX_COLUMN_CHECK_RETRIES}",
                    )
                    if retries > MAX_COLUMN_CHECK_RETRIES:
                        log_node_event(
                            "advisor_agent",
                            "草稿字段检索校验连续失败，跳过本轮落盘",
                        )
                        break
                    # 增量纠正：只提示缺失表并让模型补齐检索
                    agent_history = list(
                        new_history[:persist_start_index]
                    )
                    agent_history.append(HumanMessage(
                        content=(
                            "以下目标表尚未调用 search_columns 检索字段："
                            + ", ".join(missing_tables)
                            + "。请只对这几张表调用 search_columns 核对指标、"
                            "维度和时间字段后，再调用 update_draft_plan 更新草稿。"
                        ),
                        name="internal",
                    ))
                    continue
            break

        # 如果模型没有返回结果
        if new_history is None:
            log_node_event("advisor_agent", "Agent 未返回有效结果")
            error_answer = "系统处理异常，请重试"
            return {
                "final_answer": error_answer,
                "advisor_thinking": thinking_collector.lines,
                "messages": [
                    AIMessage(
                        content=error_answer,
                        name="advisor",
                        id=f"{state['request_id']}:advisor",
                    )
                ],
            }

        last_msg = new_history[-1]
        current_round_messages = new_history[persist_start_index:]

        # 打印本轮所有 tool_calls
        all_tool_names = []
        for msg in current_round_messages:
            for tc in (getattr(msg, "tool_calls", None) or []):
                all_tool_names.append(tc.get("name", "?"))
        log_tools_called("advisor_agent", all_tool_names)

        # 处理 Advisor 更新的共享草稿方案（status=draft）
        draft_plan = None
        draft_args_list = _get_tool_call_args(
            current_round_messages,
            "update_draft_plan",
        )
        if draft_args_list:
            draft_args, _ = draft_args_list[-1]
            draft_plan = merge_draft_plan(current_plan, draft_args)

        # 最终回复：LLM 自主决定对用户说什么（问题/解释/简短告知）
        final_answer = str(getattr(last_msg, "content", "") or "").strip()
        if not final_answer:
            if draft_plan is not None:
                final_answer = "已更新当前方案的草稿，请继续补充或确认缺失的口径。"
            else:
                final_answer = _normalize_clarification_message("")

        log_node_end(
            "advisor_agent",
            answer_summary=str(final_answer)[:120],
            draft=draft_plan is not None,
            retries=retries,
            ms=elapsed_ms(timer),
        )

        # 只保存本轮新增的AI和工具消息，不重复返回完整历史
        messages_to_persist = []
        for message in new_history[persist_start_index:]:
            # 内部重试指令不属于用户对话记忆
            if isinstance(message, HumanMessage):
                continue
            # Agent原始最终回复由带name的标准消息代替
            if message is last_msg:
                continue
            messages_to_persist.append(message)

        # 保存统一的Advisor可见回复，供Planner读取
        messages_to_persist.append(AIMessage(
            content=final_answer,
            name="advisor",
            id=f"{state['request_id']}:advisor",
        ))

        return_value = {
            "messages": messages_to_persist,
            # Advisor 每执行一次，澄清轮次增加一次
            "advisor_turns": state.get("advisor_turns", 0) + 1,
            "final_answer": final_answer,
            # ReAct 每步 LLM 输出，供前端思考过程展示
            "advisor_thinking": thinking_collector.lines,
            # Advisor 只负责澄清与维护草稿，本轮结束后等 Planner 再判定
            "topic_status": "clarifying",
        }

        if draft_plan is not None:
            # 草稿以 draft 状态持久化，供 Planner 下一轮合并收尾
            return_value["confirmed_plan"] = draft_plan

        # 节点完成后记录 State 分层摘要，供 trace 查看数据流转
        log_state_snapshot("advisor", {**state, **return_value})

        return return_value

    # 简单单节点子图：START -> run_advisor -> END
    graph.add_node("run_advisor", run_advisor)
    graph.add_edge(START, "run_advisor")
    graph.add_edge("run_advisor", END)
    return graph.compile()
