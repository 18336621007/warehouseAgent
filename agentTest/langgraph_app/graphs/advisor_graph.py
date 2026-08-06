# Advisor 子图：ReAct Agent，内部用工具查元数据，对外说业务语言
# 一问一答模式，不保留内部状态机。Planner 是唯一的调度中心。
# 企业级三层防护：Prompt 指引 → 图级拦截(硬保障) → 可观测日志
# Advisor 提交完整方案后，由领域服务统一生成 status=locked 的标准查询方案
# 图级校验确保提交方案前已检索目标表字段，禁止根据回复文本自动补充字段
import json

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from agentTest.langgraph_app.state.advisor_state import AdvisorState
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.graph_logger import log_node_end, start_timer, log_node_start, elapsed_ms, log_node_event
from agentTest.langgraph_app.runtime.graph_logger import log_tools_called, log_example_retrieved, log_plan_locked, log_advisor_mode
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools
from agentTest.langgraph_app.prompts.advisor_prompt import ADVISOR_SYSTEM_PROMPT
from agentTest.config.advisor import MAX_COLUMN_CHECK_RETRIES
from langchain_core.messages import HumanMessage, AIMessage
from agentTest.langgraph_app.services.query_plan_service import lock_query_plan
from agentTest.langgraph_app.services.metric_ambiguity_validator import MetricAmbiguityValidator
from agentTest.langgraph_app.services.metric_clarification_service import MetricClarificationService



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

def _build_confirmation_message(plan: dict, semantic_labels: dict = None) -> str:
    """用locked_plan构造简洁确认消息：指标带中文含义与字段名，维度只列中文含义。"""
    tables = plan.get("tables") or []
    measures = plan.get("measures", [])
    dimensions = plan.get("dimensions", [])
    time_field = plan.get("time_field", "pt_dt")
    time_range = plan.get("time_range", "") or "昨天"
    filters = plan.get("filters", "")
    labels = semantic_labels or {}

    # ???????????????????????????????
    concept_resolutions = plan.get("concept_resolutions") or {}
    resolved_metric_fields = [
        info.get("field", "")
        for info in concept_resolutions.values()
        if isinstance(info, dict) and info.get("field")
    ]
    if resolved_metric_fields:
        resolved_set = set(resolved_metric_fields)
        filtered_measures = [m for m in measures if m in resolved_set]
        if filtered_measures:
            measures = list(dict.fromkeys(filtered_measures))

    def _table_name(table: str) -> str:
        """表中文含义优先，其次表短名。"""
        meaning = labels.get("tables", {}).get(table, "")
        if meaning:
            return meaning
        return table.split(".")[-1] if "." in table else table

    def _field_label(field: str) -> str:
        """指标：中文含义（字段名）。"""
        meaning = labels.get("fields", {}).get(field, "")
        return f"{meaning}（{field}）" if meaning else field

    def _dim_label(field: str) -> str:
        """维度：只列中文含义，减少文字量；无含义时回退字段名。"""
        meaning = labels.get("fields", {}).get(field, "")
        return meaning or field

    lines = ["查询方案确认："]
    lines.append(f"- 表：{'、'.join(_table_name(t) for t in tables)}")
    if measures:
        lines.append(f"- 指标：{'、'.join(_field_label(m) for m in measures)}")
    if dimensions:
        # 确认方案只展示最终锁定的内容，维度完整列出，不出现候选式省略
        lines.append(f"- 维度：{'、'.join(_dim_label(d) for d in dimensions)}")
    lines.append(f"- 时间：{time_range}（{time_field}）")
    if filters:
        lines.append(f"- 过滤：{filters}")
    lines.append("")
    lines.append("回复“好”开始查询。")
    return "\n".join(lines)


def _normalize_clarification_message(message: str) -> str:
    """未生成locked_plan时统一标记为澄清信息，避免用户误以为可以执行。"""
    content = (message or "").strip()
    misleading_phrases = (
        "已确认查询方案如下",
        "已确认所需字段和时间维度",
        "已确认所需字段",
        "查询方案已确认",
        "方案已锁定",
        "请确认是否执行",
        "回复确认开始查询",
    )

    for phrase in misleading_phrases:
        content = content.replace(phrase, "当前已识别的信息如下")

    if not content:
        return "当前仍有查询口径需要确认，请补充最关键的指标、维度或过滤条件。"

    # 只有真实locked_plan才能请求最终执行确认。
    return "当前方案尚未锁定，以下内容仅用于继续核对：\n" + content


def _extract_field_meaning(page_content: str) -> str:
    """从字段元数据文本提取中文含义：优先原始备注，其次首个别名，避免展示长别名列表。"""
    for line in (page_content or "").splitlines():
        if line.startswith("原始备注:"):
            return line[len("原始备注:"):].strip()[:60]
    for line in (page_content or "").splitlines():
        if line.startswith("别名:"):
            aliases = line[len("别名:"):].strip()
            if aliases:
                return aliases.split("、")[0].strip()[:60]
    return ""


def _extract_table_meaning(page_content: str) -> str:
    """从表元数据文本提取中文含义：优先核心功能，其次所属领域。"""
    for line in (page_content or "").splitlines():
        if line.startswith("核心功能:"):
            return line[len("核心功能:"):].strip()
    for line in (page_content or "").splitlines():
        if line.startswith("所属领域:"):
            return line[len("所属领域:"):].strip()
    return ""


def _extract_aliases_from_comment(page_content: str) -> str:
    """从字段元数据文本提取别名摘要（前 2 个），检索阶段保留别名供 LLM 核验。"""
    for line in (page_content or "").splitlines():
        if line.startswith("别名:"):
            aliases = [
                alias.strip()
                for alias in line[len("别名:"):].split("、")
                if alias.strip()
            ]
            return "、".join(aliases[:2])
    return ""


def _lookup_semantic_labels(runtime, plan: dict) -> dict:
    """按表名/字段名从真实元数据解析中文含义，供确认消息展示。"""
    labels = {"tables": {}, "fields": {}}
    column_vs = runtime.get("column_vector_store") if runtime else None

    # 表描述只使用表自带备注（original_comment），不使用增强后的长备注
    try:
        from agentTest.metadata.mysql_store import load_enriched_tables
        enriched_tables = load_enriched_tables()
    except Exception:
        enriched_tables = {}

    for table in plan.get("tables") or []:
        table_comment = str(
            enriched_tables.get(table, {}).get("original_comment") or ""
        )
        if table_comment:
            labels["tables"][table] = table_comment

    for field in list(plan.get("measures") or []) + list(plan.get("dimensions") or []):
        if column_vs is None:
            continue
        try:
            docs = column_vs.similarity_search_with_score(field, k=8)
            for doc, _ in docs:
                if str(doc.metadata.get("column") or "").lower() == str(field).lower():
                    labels["fields"][field] = _extract_field_meaning(doc.page_content or "")
                    break
        except Exception:
            continue
    return labels



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

    # 指标歧义门禁：基于真实元数据候选和用户选择证据，拦截未确认口径的方案提交
    metric_validator = MetricAmbiguityValidator(
        column_vector_store=runtime["column_vector_store"],
    )

    # 方案模式允许提交完整方案
    plan_agent = create_agent(
        llm,
        tools,
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

        # ── 检索历史优质示例：指标未解析前只注入问题摘要，不注入SQL，避免历史案例锚定业务口径 ──
        example_vs = runtime.get("example_vector_store")
        example_context = ""
        examples = []
        current_spec = state.get("analysis_spec") or {}
        has_unresolved_metric = any(
            resolution.get("status") != "resolved"
            for resolution in (current_spec.get("metric_resolutions") or [])
        )
        if example_vs and question and (is_first_advisor_turn or has_unresolved_metric):
            examples = example_vs.search_similar(question, k=2)
            if examples:
                lines_ex = ["【历史相似问题（仅供参考，不能代替当前用户确认口径）】"]
                for i, doc in enumerate(examples, 1):
                    q = doc.metadata.get("question", "")
                    lines_ex.append(f"{i}. 问题：{q}")
                example_context = "\n".join(lines_ex)
                top_q = examples[0].metadata.get("question","")[:50] if examples else ""
                sim_val = examples[0].metadata.get("_similarity","?") if examples else "?"
                log_example_retrieved(
                    "advisor_agent",
                    hit_count=len(examples),
                    top_sim=sim_val,
                    top_question=top_q,
                    hint="指标未解析，不注入历史SQL",
                )

        # 将 Planner 分析结果和当前旧方案作为结构化上下文提供给 Advisor
        planner_tables = planner_entities.get("tables") or []
        planner_fields = planner_entities.get("fields") or []
        planner_completeness = planner_entities.get("completeness", "")
        planner_reason = state.get("planner_reason", "")

        # Advisor 可以在本轮工具核验后直接锁定方案，避免用户额外发送一次消息触发锁定。
        agent = plan_agent
        advisor_mode = "plan" if planner_completeness == "full" else "adaptive"

        log_advisor_mode(
            "advisor_agent",
            mode=advisor_mode,
            completeness=planner_completeness or "none",
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
                if planner_completeness == "full"
                else
                "Planner仅完成初步判断。请先使用工具核验；如果存在多个业务口径，"
                "只向用户询问一个关键问题；如果用户本轮已解决歧义且字段唯一，"
                "必须在本轮直接调用submit_query_plan，不要再让用户发送确认来触发锁定。"
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

        metric_resolution_context = MetricClarificationService.build_resolution_context(
            current_spec
        )
        if metric_resolution_context:
            # 将程序已解析口径注入本轮上下文，避免 Advisor 再次依赖自然语言猜测。
            context_lines.extend(["", metric_resolution_context])

        msg_content = "\n".join(context_lines)

        if example_context:
            msg_content = example_context + "\n\n" + msg_content



        # 临时增强本轮用户消息，但不把检索上下文写入Checkpoint
                # 构建 Planner 候选池摘要，注入消息上下文
        from langchain_core.messages import SystemMessage
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

        agent_history = list(history)
        if candidate_text:
            agent_history.insert(0, SystemMessage(content=candidate_text))
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
        log_tools_called("advisor_agent", all_tool_names)


        # 处理 Advisor 提交的完整查询方案
        locked_plan = None
        plan_validation_error = ""

        submit_args_list = _get_tool_call_args(
            current_round_messages,
            "submit_query_plan",
        )

        # 指标歧义门禁结果，None 表示未启用或未触发
        resolution_result = None
        ambiguity_result = None
        resolved_concept_resolutions = {}

        # 指标歧义门禁公共输入：AnalysisSpec 指标概念，供提交校验与澄清候选共用
        current_spec = state.get("analysis_spec") or {}
        metric_mentions = current_spec.get("metric_mentions") or []

        def _metric_gate_inputs(submit_tables: list = None) -> tuple:
            """构造指标门禁输入：目标表（提交表优先）与优秀案例命中字段。"""
            target_tables = list(dict.fromkeys(
                list(submit_tables or [])
                + list(current_plan.get("tables") or [])
            ))
            if not target_tables:
                target_tables = list(planner_entities.get("tables") or [])
            # 优秀案例命中字段仅用于候选排序加权，不产生解析证据
            example_fields = []
            for doc in examples:
                raw_fields = doc.metadata.get("fields", "[]")
                try:
                    example_fields.extend(json.loads(raw_fields) or [])
                except Exception:
                    continue
            return target_tables, example_fields

        if submit_args_list and not submission_blocked:
            args, _ = submit_args_list[-1]

            # ── 程序级指标歧义门禁：信任 LLM 提交的解析字段，程序只校验字段合法性 ──
            submitted_resolutions = list(args.get("concept_resolutions") or [])

            if metric_mentions:
                # 目标表优先收敛候选：本轮提交表 + 已确认方案表，缺失时回退 Planner 候选表
                metric_target_tables, metric_example_fields = _metric_gate_inputs(
                    args.get("tables") or []
                )
                resolution_result = metric_validator.validate(
                    metric_mentions=metric_mentions,
                    planner_candidates=planner_entities.get("column_candidates") or [],
                    previous_resolutions=current_spec.get("metric_resolutions") or [],
                    target_tables=metric_target_tables,
                    example_fields=metric_example_fields,
                    llm_resolutions=submitted_resolutions,
                )
                if not resolution_result.resolved:
                    # 多候选且用户未选择：阻止锁定，程序生成候选选项并追问
                    ambiguity_result = resolution_result
                    log_node_event(
                        "advisor_agent",
                        f"指标歧义门禁拦截: {resolution_result.reason}",
                    )
                else:
                    # 信任 LLM 提交的解析证据，转换为可审计 concept_resolutions
                    resolved_concept_resolutions = metric_validator.to_plan_resolutions(
                        resolution_result,
                    )

            proposed_plan = {
                "tables": list(args.get("tables") or []),
                "measures": list(args.get("measures") or []),
                "dimensions": list(args.get("dimensions") or []),
                "time_field": args.get("time_field") or "pt_dt",
                "time_range": args.get("time_range") or "昨天",
                "filters": args.get("filters") or "",
                "field_sources": list(args.get("field_sources") or []),
                "order_by": list(args.get("order_by") or []),
                "having": args.get("having") or "",
                "result_limit": args.get("result_limit", 1000),
                "complex": args.get("complex", False),
                "table_plans": list(args.get("table_plans") or []),
            }
            if resolved_concept_resolutions:
                # 用户已确认口径时由程序直接覆盖 measures，避免 LLM 漏传或改写后再次追问。
                resolved_fields = [
                    info.get("field", "")
                    for info in resolved_concept_resolutions.values()
                    if isinstance(info, dict) and info.get("field")
                ]
                if resolved_fields:
                    submitted_measures = list(proposed_plan["measures"])
                    proposed_plan["measures"] = list(dict.fromkeys(resolved_fields))
                    if submitted_measures != proposed_plan["measures"]:
                        log_node_event(
                            "advisor_agent",
                            "已按用户确认口径收敛 measures: "
                            f"{submitted_measures} -> {proposed_plan['measures']}",
                        )

            if ambiguity_result is not None:
                # 指标歧义未解决或解析证据不合法时，禁止生成 locked_plan
                locked_plan = None
            else:
                try:
                    # tables、fields、状态和时间戳统一由领域服务生成
                    locked_plan = lock_query_plan(
                        proposed_plan,
                        concept_resolutions=resolved_concept_resolutions,
                    )

                    log_plan_locked(
                        "advisor_agent",
                        table=locked_plan.get("table", ""),
                        measures=locked_plan.get("measures", []),
                        dimensions=locked_plan.get("dimensions", []),
                        order_by=locked_plan.get("order_by", []),
                        result_limit=locked_plan.get("result_limit", 1000),
                        table_plans=locked_plan.get("table_plans") or [],
                    )
                except ValueError as error:
                    # 方案不完整时禁止伪装成已锁定
                    plan_validation_error = str(error)
                    log_node_event(
                        "advisor_agent",
                        f"方案锁定失败: {plan_validation_error}",
                    )

        elif metric_mentions and not submission_blocked:
            # LLM 未提交方案时：存在未解析指标歧义也必须由程序生成澄清候选，
            # 口径名称以原始备注为准，禁止 LLM 自由编造
            metric_target_tables, metric_example_fields = _metric_gate_inputs()
            pre_result = metric_validator.validate(
                metric_mentions=metric_mentions,
                planner_candidates=planner_entities.get("column_candidates") or [],
                previous_resolutions=current_spec.get("metric_resolutions") or [],
                target_tables=metric_target_tables,
                example_fields=metric_example_fields,
            )
            if not pre_result.resolved:
                # 程序生成澄清候选并覆盖 LLM 自由追问文本，避免口径名称被改写
                ambiguity_result = pre_result

        elif submission_blocked:
            plan_validation_error = "提交方案前未完成目标表字段检索"

        if ambiguity_result is not None:
            # 程序生成的指标口径澄清选项，不展示 LLM 原文
            final_answer = MetricClarificationService.build_clarification_message(
                ambiguity_result
            )
        elif locked_plan:
            # 只展示经过程序校验的 locked 方案
            # 确认消息附带真实元数据的中文含义（字段/表），避免只给名称
            final_answer = _build_confirmation_message(
                locked_plan,
                _lookup_semantic_labels(runtime, locked_plan),
            )
        elif plan_validation_error:
            final_answer = (
                "当前分析方案还不完整，我需要继续确认指标、"
                "维度、时间范围或过滤条件。"
            )
        elif planner_completeness == "full":
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
            # 澄清阶段统一标识为未锁定，避免用户误以为已经进入最终确认。
            final_answer = _normalize_clarification_message(
                last_msg.content if last_msg.content else ""
            )

        # 日志记录最终对用户可见回复，避免将 LLM 原始草稿误认为实际输出。
        log_node_end(
            "advisor_agent",
            answer_summary=str(final_answer)[:120],
            locked=locked_plan is not None,
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

        # 提交校验和预校验统一回写，首轮候选必须固化后才能可靠解释下一轮编号。
        effective_result = resolution_result or ambiguity_result
        if effective_result is not None:
            return_value["analysis_spec"] = MetricClarificationService.update_analysis_spec(
                state.get("analysis_spec") or {},
                effective_result,
                state.get("request_id", ""),
            )


        return return_value

    graph.add_node("advisor_agent", run_advisor)
    graph.add_edge(START, "advisor_agent")
    graph.add_edge("advisor_agent", END)

    return graph.compile()




