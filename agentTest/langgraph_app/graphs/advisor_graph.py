# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
# 一问一答模式，不保留内部状态机。Planner 是唯一的调度中心。
# 企业级三层防护：Prompt 指引 → 图级拦截(硬保障) → 可观测日志
# 新增：confirm_selection 参数重构后，图级自动合并 measures/dimensions/time_field/filters
# 新增：图级兜底校验 —— 从 search_columns 返回中提取字段名，自动补齐遗漏维度
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage, AIMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.state.advisor_state import AdvisorState
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms, log_node_event
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from datetime import datetime
from agentTest.config.advisor import MAX_COLUMN_CHECK_RETRIES


def _extract_fields_from_search_results(messages: list) -> set:
    """从本轮 search_columns / search_tables 的返回结果中提取所有字段名。
    
    工具返回格式为 LLM 可读文本，每行包含字段名、类型、描述等信息。
    这里用简单规则提取：匹配 ToolMessage 中出现的字段名模式。"""
    field_names = set()
    for msg in messages:
        if isinstance(msg, ToolMessage):
            content = msg.content
            for line in content.split("\n"):
                line = line.strip()
                # 匹配常见字段名格式
                for prefix in ["字段:", "字段名:", "列名:", "column:"]:
                    if prefix in line:
                        parts = line.split(prefix, 1)
                        if len(parts) > 1:
                            name = parts[1].strip().split()[0].strip("()（），,")
                            if name and not name.startswith("---"):
                                field_names.add(name)
    return field_names


def _get_tool_call_args(messages, tool_name):
    """获取指定工具调用的参数，返回列表 [(args_dict, tool_call_id), ...]"""
    results = []
    for msg in messages:
        for tc in (getattr(msg, "tool_calls", None) or []):
            if tc.get("name") == tool_name:
                results.append((tc.get("args", {}), tc.get("id", "")))
    return results


def _build_confirmation_message(plan: dict) -> str:
    """用confirmed_plan构造标准化确认消息，杜绝LLM编造查询结果"""
    table = plan.get("table", "")
    measures = plan.get("measures", [])
    dimensions = plan.get("dimensions", [])
    time_field = plan.get("time_field", "pt_dt")
    time_range = plan.get("time_range", "") or "昨天"
    filters = plan.get("filters", "")

    lines = ["已锁定分析方案：", f"- 数据表：{table}"]
    if measures:
        lines.append(f"- 度量：{', '.join(measures)}")
    if dimensions:
        lines.append(f"- 维度：{', '.join(dimensions)}")
    lines.append(f"- 时间：{time_field} = {time_range}")
    if filters:
        lines.append(f"- 过滤：{filters}")
    lines.append("")
    lines.append('以上信息确认无误？回复"好"开始查询。')
    return "\n".join(lines)


def build_advisor_subgraph(runtime):
    """构建 Advisor ReAct Agent 子图 —— 一问一答，含 confirm_selection 合规校验"""
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

    agent = create_agent(llm, tools, system_prompt=ADVISOR_SYSTEM_PROMPT)

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
        用 current_user_input（用户原话）理解选择，如"1"/"月租订单"/"好的"；
        Planner 用 original_question 保证语义完整，两者互不干扰。
        企业级防护：confirm_selection 前必须调过 search_columns，否则拦截重跑。
        图级兜底：从 search_columns 返回中提取字段名，自动补齐遗漏维度。"""
        # Advisor 使用本轮输入理解用户的选择、补充或确认
        question = state.get("current_user_input", state["original_question"])
        timer = start_timer()
        log_node_start("advisor_agent", question=question[:60])

        history = list(state.get("advisor_messages") or [])

        # ── 新增：检索历史优质示例，加速澄清 ──
        example_vs = runtime.get("example_vector_store")
        example_context = ""
        if example_vs and question and not history:
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

        # Planner候选表注入，Advisor在此基础上做字段检索
        planner_tables = (state.get("planner_entities") or {}).get("tables", [])
        if planner_tables and not history:
            table_ctx = "\n(Planner已筛选候选表: " + ", ".join(planner_tables) + "，请在此基础上选择）"
            msg_content = question + table_ctx
            if example_context:
                msg_content = example_context + "\n\n" + msg_content
            history.append(HumanMessage(content=msg_content))
        else:
            msg_content = question
            if example_context:
                msg_content = example_context + "\n\n" + msg_content
            history.append(HumanMessage(content=msg_content))

        new_history = None
        retries = 0

        while retries <= MAX_COLUMN_CHECK_RETRIES:
            result = agent.invoke({"messages": history})
            new_history = result["messages"]

            # confirm_selection 前必须调过 search_columns
            has_confirm = _find_tool_calls(new_history, "confirm_selection")
            has_column_search = _find_tool_calls(new_history, "search_columns")

            if has_confirm and not has_column_search:
                retries += 1
                log_node_event("advisor_agent",
                    f"拦截 confirm_selection（缺少 search_columns），重试 {retries}/{MAX_COLUMN_CHECK_RETRIES}")
                history.append(HumanMessage(
                    content="调用 confirm_selection 前必须先调用 search_columns 检索该表的所有字段。"
                            "请调用 search_columns，列出相关字段供用户选择，不要直接锁定方案。"
                ))
                continue

            break

        if new_history is None:
            log_node_event("advisor_agent", "Agent 未返回有效结果")
            return {"final_answer": "系统处理异常，请重试", "advisor_messages": history}

        last_msg = new_history[-1]

        # 打印本轮所有 tool_calls
        all_tool_names = []
        for msg in new_history:
            for tc in (getattr(msg, "tool_calls", None) or []):
                all_tool_names.append(tc.get("name", "?"))
        log_node_event("advisor_agent", f"本轮工具调用: {all_tool_names if all_tool_names else '[无]'}")

        # 处理 confirm_selection 工具调用
        confirmed_plan = None
        confirm_args_list = _get_tool_call_args(new_history, "confirm_selection")

        if confirm_args_list:
            args, tc_id = confirm_args_list[-1]

            # 解析新参数结构
            table = args.get("table", "")
            measures = args.get("measures", [])
            dimensions = args.get("dimensions", [])
            time_field = args.get("time_field", "pt_dt")
            time_range = args.get("time_range", "")
            filters = args.get("filters", "")

            # 图级兜底校验：从 search_columns 返回中提取字段名，自动补齐遗漏维度
            all_fields_from_search = _extract_fields_from_search_results(new_history)

            # 从 Advisor 文本回复和 search_columns 返回中交叉验证
            mentioned_fields = set()
            for msg in new_history:
                if isinstance(msg, AIMessage) and msg.content:
                    text = msg.content
                    for field_name in all_fields_from_search:
                        if field_name in text:
                            mentioned_fields.add(field_name)

            # 补齐遗漏的维度字段
            missing_dims = []
            for field in mentioned_fields:
                if (field in all_fields_from_search
                        and field not in measures
                        and field not in dimensions
                        and field != time_field):
                    missing_dims.append(field)

            if missing_dims:
                log_node_event("advisor_agent",
                    f"图级兜底: 自动补齐遗漏维度 {missing_dims}")
                dimensions = list(dimensions) + missing_dims

            # 构建 confirmed_plan
            all_fields = list(measures) + list(dimensions) + [time_field]
            seen = set()
            all_fields_dedup = []
            for f in all_fields:
                if f not in seen:
                    seen.add(f)
                    all_fields_dedup.append(f)

            confirmed_plan = {
                "table": table,
                "tables": [table],
                "measures": list(measures),
                "dimensions": list(dimensions),
                "time_field": time_field,
                "time_range": time_range,
                "filters": filters,
                "fields": all_fields_dedup,
                "confirmed_at": datetime.now().isoformat(),
            }

            # 写入 ToolMessage
            new_history.append(ToolMessage(
                content=f"已确认: {args}",
                tool_call_id=tc_id,
            ))

            log_node_event("advisor_agent",
                f"confirmed_plan: table={table}, measures={measures}, "
                f"dimensions={dimensions}, time={time_field}({time_range or '未指定'}), "
                f"filters={filters or '无'}, missing_dims={missing_dims if missing_dims else '无'}")

        log_node_end("advisor_agent",
                     answer_summary=str(last_msg.content)[:120] if last_msg.content else "",
                     confirmed=confirmed_plan is not None,
                     retries=retries,
                     ms=elapsed_ms(timer))


        if confirmed_plan:
            # 确认阶段：用标准化消息代替LLM原文，杜绝编造假结果
            final_answer = _build_confirmation_message(confirmed_plan)

        else:
            # 澄清阶段：保留LLM原文
            final_answer = last_msg.content if last_msg.content else ""


        return_value = {
            "advisor_messages": new_history,

            # Advisor 每执行一次，澄清轮次增加一次
            "advisor_turns": state.get("advisor_turns", 0) + 1,

            # 保存本轮回复，供下一轮 Planner 理解“1”“第二个”等简短回答
            "advisor_last_answer": final_answer,
            "final_answer": final_answer,
        }

        if confirmed_plan:
            return_value["confirmed_plan"] = confirmed_plan


        return return_value

    graph.add_node("advisor_agent", run_advisor)
    graph.add_edge(START, "advisor_agent")
    graph.add_edge("advisor_agent", END)

    return graph.compile()
