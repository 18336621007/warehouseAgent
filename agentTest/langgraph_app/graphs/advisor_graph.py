# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
# 一问一答模式，不保留内部状态机。Planner 是唯一的调度中心。
# 企业级三层防护：Prompt 指引 → 图级拦截(硬保障) → 可观测日志
# Advisor 提交完整方案后，由领域服务统一生成 status=locked 的标准查询方案
# 图级校验确保提交方案前已检索目标表字段，禁止根据回复文本自动补充字段
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.state.advisor_state import AdvisorState
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms, log_node_event
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from agentTest.config.advisor import MAX_COLUMN_CHECK_RETRIES
from langchain_core.messages import HumanMessage, AIMessage
from agentTest.langgraph_app.services.query_plan_service import lock_query_plan



def _get_tool_call_args(messages, tool_name):
    """获取指定工具调用的参数，返回列表 [(args_dict, tool_call_id), ...]"""
    results = []
    for msg in messages:
        for tc in (getattr(msg, "tool_calls", None) or []):
            if tc.get("name") == tool_name:
                results.append((tc.get("args", {}), tc.get("id", "")))
    return results

def _has_column_search_for_tables(messages, tables: list[str]) -> bool:
    """检查 Advisor 是否对所有目标表都检索过字段"""
    if not tables:
        return False
    search_args_list = _get_tool_call_args(
        messages,
        "search_columns",
    )
    searched = {
        args.get("table", "")
        for args, _ in search_args_list
        if args.get("table")
    }
    return all(t in searched for t in tables)

def _build_confirmation_message(plan: dict) -> str:
    """用locked_plan构造标准化确认消息，杜绝LLM编造查询结果"""
    tables = plan.get("tables") or []
    measures = plan.get("measures", [])
    dimensions = plan.get("dimensions", [])
    time_field = plan.get("time_field", "pt_dt")
    time_range = plan.get("time_range", "") or "昨天"
    filters = plan.get("filters", "")
    field_sources = plan.get("field_sources") or {}

    lines = ["已锁定分析方案：", f"- 数据表：{', '.join(tables)}"]
    if measures:
        lines.append(f"- 度量：{', '.join(measures)}")
    if dimensions:
        lines.append(f"- 维度：{', '.join(dimensions)}")
    lines.append(f"- 时间：{time_field} = {time_range}")
    if filters:
        lines.append(f"- 过滤：{filters}")
    if len(tables) > 1 and field_sources:
        lines.append("- 字段来源：")
        for f, t in field_sources.items():
            lines.append(f"  {f} \u2190 {t}")
    lines.append("")
    lines.append('以上信息确认无误？回复"好"开始查询。')
    return "\n".join(lines)


def build_advisor_subgraph(runtime):
    """构建 Advisor ReAct Agent 子图 —— 一问一答，含方案提交合规校验。"""
    llm = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,
    )

    tools = build_advisor_tools(
        runtime["db_vector_store"],
        runtime["table_vector_store"],
        runtime["column_vector_store"],
    )

    # 方案模式允许提交完整方案
    plan_agent = create_agent(
        llm,
        tools,
        system_prompt=ADVISOR_SYSTEM_PROMPT,
    )

    # 澄清模式不绑定方案提交工具，保证模糊需求无法在本轮锁定
    clarification_tools = [
        advisor_tool for advisor_tool in tools if advisor_tool.name != "submit_query_plan"
    ]
    clarification_agent = create_agent(
        llm,
        clarification_tools,
        system_prompt=ADVISOR_SYSTEM_PROMPT,
    )

    graph = StateGraph(AdvisorState)

    def _find_tool_calls(messages, tool_name):
        """检查消息列表中是否包含指定工具的调用"""
        for msg in messages:
            for tc in (getattr(msg, "tool_calls", None) or []):
                if tc.get("name") == tool_name:
                    return True
        return False

    def run_advisor(state):
        """处理用户问题：基于完整对话历史生成回复。
        Planner 提供还原后的完整需求，current_user_input 保留用户本轮原话；
        Advisor 负责澄清或提交完整方案，不负责用户最终执行确认；
        企业级防护：submit_query_plan 前必须检索目标表字段，否则拦截重跑。"""
        # Planner 已将“1”“A”等短回答还原为完整有效需求
        planner_entities = state.get("planner_entities") or {}
        effective_query = (
                planner_entities.get("effective_query")
                or state.get("original_question")
                or state.get("current_user_input", "")
        )
        current_user_input = state.get("current_user_input", "")
        question = effective_query
        timer = start_timer()
        log_node_start("advisor_agent", question=question[:60])

        # messages是当前Topic唯一的标准消息历史
        history = list(state.get("messages") or [])
        advisor_turns = state.get("advisor_turns", 0)
        is_first_advisor_turn = advisor_turns == 0

        # ── 新增：检索历史优质示例，加速澄清 ──
        example_vs = runtime.get("example_vector_store")
        example_context = ""
        if example_vs and question and is_first_advisor_turn:
            examples = example_vs.search_similar(question, k=2)
            if examples:
                lines_ex = ["【历史相似问题（曾成功解决，仅供参考）】"]
                for i, doc in enumerate(examples, 1):
                    q = doc.metadata.get("question", "")
                    s = doc.metadata.get("sql", "")[:200]
                    lines_ex.append(f"{i}. 问题：{q}")
                    lines_ex.append(f"   对应SQL：{s}...")
                example_context = "\n".join(lines_ex)
                top_q = examples[0].metadata.get("question","")[:50] if examples else ""
                sim_val = examples[0].metadata.get("_similarity","?") if examples else "?"
                log_node_event("advisor_agent", f"优秀示例检索: 命中{len(examples)}条, top_sim={sim_val}, q={top_q}")

        # 将 Planner 分析结果和当前旧方案作为结构化上下文提供给 Advisor
        planner_tables = planner_entities.get("tables") or []
        planner_fields = planner_entities.get("fields") or []
        planner_completeness = planner_entities.get("completeness", "")
        planner_reason = state.get("planner_reason", "")

        # 只有 Planner 判定需求完整时才允许 Advisor 提交方案
        can_submit_plan = planner_completeness == "full"
        if can_submit_plan:
            agent = plan_agent
            advisor_mode = "plan"
        else:
            agent = clarification_agent
            advisor_mode = "clarify"

        log_node_event(
            "advisor_agent",
            f"模式={advisor_mode} | "
            f"completeness={planner_completeness or 'none'}",
        )

        # confirmed_plan 字段同时承载 locked 和 confirmed 两种完整方案状态
        current_plan = state.get("confirmed_plan") or {}

        context_lines = [
            "【Planner还原的当前完整需求】",
            effective_query,
            "",
            "【用户本轮原始输入】",
            current_user_input,
        ]

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

        context_lines.extend([
            "",
            "【本轮操作模式】",
            (
                "需求已经完整，可以在核对元数据后提交完整方案。"
                if can_submit_plan
                else
                "需求仍存在歧义，本轮只能检索并向用户澄清，"
                "不得提交或锁定查询方案。"
            ),
        ])

        if current_plan:
            context_lines.extend([
                "",
                "【当前已有方案】",
                f"状态：{current_plan.get('status', '未设置')}",
                f"数据表：{current_plan.get('table', '未设置')}",
                f"度量：{', '.join(current_plan.get('measures') or []) or '无'}",
                f"维度：{', '.join(current_plan.get('dimensions') or []) or '无'}",
                f"时间字段：{current_plan.get('time_field', '未设置')}",
                f"时间范围：{current_plan.get('time_range', '未设置')}",
                f"过滤条件：{current_plan.get('filters', '') or '无'}",
                "用户可能只修改了部分内容，未修改内容应尽可能保留；"
                "如果整体目标已经变化，可以重新构建完整方案。",
            ])

        msg_content = "\n".join(context_lines)

        if example_context:
            msg_content = example_context + "\n\n" + msg_content



        # 临时增强本轮用户消息，但不把检索上下文写入Checkpoint
        agent_history = list(history)
        if agent_history and isinstance(agent_history[-1], HumanMessage):
            current_user_message = agent_history[-1]
            agent_history[-1] = HumanMessage(
                content=msg_content,
                name=current_user_message.name,
                id=current_user_message.id,
            )
        else:
            agent_history.append(HumanMessage(
                content=msg_content,
                name="user",
                id=f"{state['request_id']}:user",
            ))

        # 只持久化Agent调用后新增的消息
        persist_start_index = len(agent_history)

        new_history = None
        retries = 0
        submission_blocked = False

        while retries <= MAX_COLUMN_CHECK_RETRIES:
            result = agent.invoke({
                "messages": agent_history,
            })
            new_history = result["messages"]

            # submit_query_plan 必须来自本轮，目标表字段检索允许复用当前 Topic 历史
            current_round_messages = new_history[persist_start_index:]
            submit_args_list = _get_tool_call_args(
                current_round_messages,
                "submit_query_plan",
            )

            if submit_args_list and not submission_blocked:
                submit_args, _ = submit_args_list[-1]
                proposed_tables = submit_args.get("tables") or []

                has_target_column_search = _has_column_search_for_tables(
                    new_history,
                    proposed_tables,
                )

                if not has_target_column_search:
                    retries += 1
                    log_node_event(
                        "advisor_agent",
                        "拦截 submit_query_plan："
                        f"尚未检索所有目标表 {proposed_tables} 的字段，"
                        f"重试 {retries}/{MAX_COLUMN_CHECK_RETRIES}",
                    )

                    # 达到重试上限后阻止本轮方案进入锁定流程
                    if retries > MAX_COLUMN_CHECK_RETRIES:
                        submission_blocked = True
                        log_node_event(
                            "advisor_agent",
                            "方案提交已阻止：目标表字段检索校验连续失败",
                        )
                        break

                    # 丢弃本次不合规方案，要求 Agent 先检索目标表字段
                    agent_history = list(
                        new_history[:persist_start_index]
                    )
                    agent_history.append(HumanMessage(
                        content=(
                            "提交完整方案前，必须先调用 search_columns，"
                            f"并将 tables 明确设置为 {proposed_tables}。"
                            "请核对该表的指标、维度和时间字段后再提交方案。"
                        ),
                        name="internal",
                    ))
                    continue
            break

        if new_history is None:
            log_node_event("advisor_agent", "Agent 未返回有效结果")
            error_answer = "系统处理异常，请重试"
            return {
                "final_answer": error_answer,
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
        log_node_event("advisor_agent", f"本轮工具调用: {all_tool_names if all_tool_names else '[无]'}")


        # 处理 Advisor 提交的完整查询方案
        locked_plan = None
        plan_validation_error = ""

        submit_args_list = _get_tool_call_args(
            current_round_messages,
            "submit_query_plan",
        )

        if submit_args_list and not submission_blocked:
            args, _ = submit_args_list[-1]

            proposed_plan = {
                "tables": list(args.get("tables") or []),
                "measures": list(args.get("measures") or []),
                "dimensions": list(args.get("dimensions") or []),
                "time_field": args.get("time_field") or "pt_dt",
                "time_range": args.get("time_range") or "昨天",
                "filters": args.get("filters") or "",
                "field_sources": list(args.get("field_sources") or []),
            }

            try:
                # tables、fields、状态和时间戳统一由领域服务生成
                locked_plan = lock_query_plan(proposed_plan)

                log_node_event(
                    "advisor_agent",
                    "locked_plan: "
                    f"table={locked_plan.get('table', '')}, "
                    f"measures={locked_plan.get('measures', [])}, "
                    f"dimensions={locked_plan.get('dimensions', [])}, "
                    f"time={locked_plan.get('time_field', '')}"
                    f"({locked_plan.get('time_range', '') or '未指定'}), "
                    f"filters={locked_plan.get('filters', '') or '无'}",
                )
            except ValueError as error:
                # 方案不完整时禁止伪装成已锁定
                plan_validation_error = str(error)
                log_node_event(
                    "advisor_agent",
                    f"方案锁定失败: {plan_validation_error}",
                )

        elif submission_blocked:
            plan_validation_error = "提交方案前未完成目标表字段检索"

        log_node_end(
            "advisor_agent",
            answer_summary=str(last_msg.content)[:120] if last_msg.content else "",
            locked=locked_plan is not None,
            retries=retries,
            ms=elapsed_ms(timer),
        )

        if locked_plan:
            # 只展示经过程序校验的 locked 方案
            final_answer = _build_confirmation_message(locked_plan)
        elif plan_validation_error:
            final_answer = (
                "当前分析方案还不完整，我需要继续确认指标、"
                "维度、时间范围或过滤条件。"
            )
        elif can_submit_plan:
            # Plan 模式下 Agent 未调用 submit_query_plan，禁止展示 LLM 原文伪装方案已锁定
            log_node_event(
                "advisor_agent",
                "Plan模式下Agent未调用submit_query_plan，拒绝展示未锁定方案的LLM文本",
            )
            final_answer = (
                "我已经分析了您的查询需求，但在形成正式方案时遇到了问题。"
                "您的需求已被记录，请重新发送消息，我会继续处理。"
            )
        else:
            # 澄清阶段保留 LLM 原文
            final_answer = last_msg.content if last_msg.content else ""


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

        # 保存统一的Advisor可见回复，供Planner和Evaluator读取
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

            # Advisor 返回后都需要等待用户继续补充或确认
            "topic_status": "clarifying",
        }

        if locked_plan:
            # State 字段沿用 confirmed_plan，具体阶段由 plan.status 区分
            return_value["confirmed_plan"] = locked_plan


        return return_value

    graph.add_node("advisor_agent", run_advisor)
    graph.add_edge(START, "advisor_agent")
    graph.add_edge("advisor_agent", END)

    return graph.compile()




