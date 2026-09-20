# execute_query_tool.py —— 查数工具：纯执行 SQL + 安全 + 落盘 + 返回结果
# 单 Agent（Codex 模式）下，Planner 自己基于语义层口径与来源表字段生成 SQL，
# 调 execute_query 执行；工具内部不做任何 LLM 决策，只保证安全（只读/白名单/
# LIMIT/分区）与结果落盘；失败/0 行返回具体信息，由 Planner 自行决定下一步。
# steps 多段查询并行/串行执行并逐段落盘（仿 Codex 查询脚本），保留查询脚本审计。
import re
import json
import threading
from contextvars import ContextVar

from langchain.tools import tool

from agentTest.langchain_app.utils.sql_cleaner import clear_sql
from agentTest.datasource.registry import resolve_engine_candidates
from agentTest.langgraph_app.runtime.graph_logger import log_sub_info

# 当前请求上下文：由 planner 循环入口设置，工具内部据此组装落盘 state（日志/落盘归属）
_current_request_id = ContextVar("execute_query_request_id", default="")
_current_conversation_id = ContextVar("execute_query_conversation_id", default="")
_current_topic_id = ContextVar("execute_query_topic_id", default="")

# 本轮实际执行过的 SQL 收集（线程安全）：Planner 结束循环后取回并透传前端展示。
# 只做"收集+透传"，不做任何判定；失败/0 行的 SQL 也记录，方便用户排查。
_EXECUTED_SQL_LOCK = threading.Lock()
_EXECUTED_SQLS: list = []

# LIMIT 安全兜底：SQL 未带 LIMIT 时程序追加的行数（防全表扫描），可被 result_limit 覆盖
_DEFAULT_SAFE_LIMIT = 50
# 结果摘要预览最大行数（回填给 Planner 的 markdown 表格行数）
_SUMMARY_PREVIEW_ROWS = 20


def _record_executed_sql(request_id, sql, table="", row_count=0, engine="", step_id="", failed=False):
    """记录一条本轮实际执行过的 SQL（线程安全，失败/0 行也记录，供前端展示）。"""
    if not sql:
        return
    with _EXECUTED_SQL_LOCK:
        _EXECUTED_SQLS.append({
            "request_id": str(request_id or ""),
            "step_id": str(step_id or ""),
            "sql": str(sql),
            "table": str(table or ""),
            "row_count": int(row_count or 0),
            "engine": str(engine or ""),
            "failed": bool(failed),
        })


def take_executed_sqls(base_request_id: str) -> list:
    """取出并清空归属于某请求的全部已执行 SQL（并行/多段子请求按前缀归并）。

    子请求 id 形如 {base}_pN / {base}_{stepId}，用 {base}_ 前缀安全归并；
    返回列表供 Planner 写入 state 回传前端，未匹配到的记录保留（防并发误清）。
    """
    base = str(base_request_id or "")
    if not base:
        return []
    with _EXECUTED_SQL_LOCK:
        kept, taken = [], []
        for _e in _EXECUTED_SQLS:
            _rid = _e.get("request_id") or ""
            if _rid == base or _rid.startswith(base + "_"):
                taken.append(_e)
            else:
                kept.append(_e)
        _EXECUTED_SQLS[:] = kept
        return taken


def set_execute_query_context(request_id: str, conversation_id: str, topic_id: str) -> tuple:
    """设置当前请求上下文，返回 contextvar tokens（调用方需 finally reset）。"""
    return (
        _current_request_id.set(str(request_id or "")),
        _current_conversation_id.set(str(conversation_id or "")),
        _current_topic_id.set(str(topic_id or "")),
    )


def reset_execute_query_context(tokens: tuple) -> None:
    """复位请求上下文，防止跨请求串会话。"""
    for _var, _token in zip(
        (_current_request_id, _current_conversation_id, _current_topic_id), tokens
    ):
        _var.reset(_token)


def _parse_steps(steps) -> list:
    """把 steps 参数解析为子查询列表：接受 JSON 数组字符串或 list，非法返回空列表。

    每段格式 {id, sql, question, result_limit}（sql 为该段独立执行的只读 SQL）。
    """
    if isinstance(steps, (list, tuple)):
        return [s for s in steps if isinstance(s, dict)]
    try:
        data = json.loads(str(steps or "").strip())
    except Exception:
        return []
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def _parse_sql_tables(sql: str) -> list[str]:
    """从 SQL 解析涉及的表全名（schema.table），用于引擎路由与跨引擎检测。"""
    try:
        from sqlglot import exp, parse_one
        expr = parse_one(sql, read="hive")
    except Exception:
        return []
    tables = []
    for table in expr.find_all(exp.Table):
        db = table.db or ""
        name = table.name
        if name:
            tables.append(f"{db}.{name}" if db else name)
    return list(dict.fromkeys(tables))


def _resolve_partition_fields(tables: list[str], semantic_provider) -> list[str]:
    """按表语义层分区字段取并集，供执行守卫识别非 pt_dt 时间分区（明细表）。

    语义层未声明分区字段（或表不在语义层）时回退默认 pt_dt。
    """
    fields = []
    if semantic_provider is not None:
        for t in tables:
            for f in (semantic_provider.get_partition_fields(t) or []):
                if f not in fields:
                    fields.append(f)
    return fields or ["pt_dt"]


def _ensure_limit(sql: str, result_limit: int) -> str:
    """SQL 缺 LIMIT 时程序安全追加（result_limit 或默认值），防全表扫描。"""
    if re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        return sql
    limit = int(result_limit) if result_limit else _DEFAULT_SAFE_LIMIT
    return f"{sql.rstrip().rstrip(';')} LIMIT {limit}"


def _build_result_summary(sql, sql_result, stored) -> str:
    """把单条查询结果组装成回填给 Planner 的摘要（列 + 预览 + 行数 + 落盘引用 + 实际 SQL）。"""
    columns = list((sql_result or {}).get("columns") or [])
    rows = list((sql_result or {}).get("rows") or [])
    row_count = int((sql_result or {}).get("row_count") or len(rows))
    lines = [f"查询成功，共 {row_count} 行（列：{', '.join(columns) or '无'}）。"]
    if rows and columns:
        _preview = rows[:_SUMMARY_PREVIEW_ROWS]
        lines.append("| " + " | ".join(columns) + " |")
        lines.append("| " + " | ".join(["---"] * len(columns)) + " |")
        for row in _preview:
            if isinstance(row, dict):
                vals = [str(row.get(c, "")) for c in columns]
            else:
                vals = [str(v) for v in row]
            lines.append("| " + " | ".join(vals) + " |")
    if stored.get("result_id"):
        lines.append(
            f"结果已落盘：{stored['result_id']}"
            f"（round {stored.get('round_no', '')}，全量 CSV：{stored.get('full_csv', '')}）"
        )
    lines.append(f"实际执行 SQL：\n{sql}")
    if row_count == 0:
        lines.append(
            "注意：本次查询 0 行——可能是过滤值与库中实际存储值不一致"
            "（可用 probe_values 探查真实取值后改 SQL 重查），也可能确实无匹配数据。"
        )
    return "\n".join(lines)


def _exec_one_sql(runtime, sql, question, request_id, step_id="", result_limit=0):
    """执行单条 SQL：路由引擎 → 安全校验（sql_query 工具内置）→ 执行 → 落盘。

    返回 (result_state, text)：result_state 供脚本元数据记录 SQL/引用，text 回填给 Agent。
    程序只保证安全与落盘，不做 SQL 生成/修复；失败/0 行由 Planner 依据返回信息自行处理。
    """
    sql = clear_sql(str(sql or ""))
    if not sql:
        return {"sql_exec_failed": True, "sql_exec_error": "SQL 为空"}, "SQL 为空，无法执行。"
    sql = _ensure_limit(sql, result_limit)
    tables = _parse_sql_tables(sql)
    if not tables:
        return (
            {"sql_exec_failed": True, "sql_exec_error": "SQL 中未解析到数据表"},
            "无法从 SQL 解析数据表，请确认 SQL 使用了白名单内的表。",
        )
    main_cands = resolve_engine_candidates(tables[0])
    if not main_cands:
        return (
            {"sql_exec_failed": True, "sql_exec_error": f"表 {tables[0]} 无可用查询引擎"},
            f"表 {tables[0]} 无可用查询引擎，请联系管理员。",
        )
    # 跨引擎检测：不同表的引擎候选不一致（如 data_project 与其余库）→ 拒绝并提示拆开
    for t in tables[1:]:
        cands = resolve_engine_candidates(t)
        if set(cands) != set(main_cands):
            return (
                {
                    "sql_exec_failed": True,
                    "sql_exec_error": f"查询涉及跨引擎表（{tables[0]} 与 {t}），暂不支持一次查询多引擎表，请拆开分别查询",
                },
                f"查询涉及跨引擎表（{tables[0]} 与 {t}），暂不支持一次查询多引擎表，请拆开分别查询。",
            )
    partition_fields = _resolve_partition_fields(tables, runtime.get("semantic_metadata_provider"))
    errors = []
    sql_result = None
    exec_engine = ""
    for engine in main_cands:
        try:
            spec = runtime["tool_registry"].get_by_name(f"sql_query_{engine}")
        except Exception:
            spec = None
        if spec is None:
            errors.append(f"[{engine}] 引擎未注册")
            continue
        try:
            sql_result = spec.tool.invoke({"sql": sql, "partition_fields": partition_fields})
            exec_engine = engine
            break
        except ValueError as error:
            # 校验失败：引擎无关，不降级（换引擎大概率同样拒绝）
            return (
                {"sql_exec_failed": True, "sql_exec_error": str(error), "sql_result": None},
                f"SQL 未通过安全校验：{error}",
            )
        except Exception as error:
            errors.append(f"[{engine}] {error}")
            continue
    if sql_result is None:
        msg = "；".join(errors) or "无可用查询引擎"
        return {"sql_exec_failed": True, "sql_exec_error": msg, "sql_result": None}, f"查询执行失败：{msg}"
    # 落盘（save_query_result 内部吞掉写盘失败，不阻断主流程）
    state = {
        "request_id": request_id,
        "conversation_id": _current_conversation_id.get(),
        "effective_query": question or sql[:120],
        "current_user_input": question or "",
        "generated_sql": sql,
        "confirmed_plan": {},
    }
    stored = {}
    try:
        from agentTest.langgraph_app.services.result_store import save_query_result
        stored = save_query_result(state, sql_result) or {}
    except Exception:
        stored = {}
    # 记录本轮实际执行过的 SQL（供前端"查看执行 SQL"展示，失败/0 行也记录）
    _record_executed_sql(
        request_id,
        sql=sql,
        step_id=step_id,
        table=", ".join(tables),
        row_count=int((sql_result or {}).get("row_count") or 0),
        engine=exec_engine,
        failed=False,
    )
    text = _build_result_summary(sql, sql_result, stored)
    result_state = {
        "sql_result": sql_result,
        "generated_sql": sql,
        "result_id": stored.get("result_id", ""),
        "result_csv": stored.get("full_csv", ""),
        "engine": exec_engine,
    }
    return result_state, text


def build_execute_query_tool(runtime):
    """构建查数工具：纯执行 SQL + 安全校验 + 落盘 + 结果摘要。"""

    def _run_steps(steps_list, fallback_question, base_request_id):
        """并行执行多段查询（每段含独立 SQL）：逐段落盘并保存查询脚本（仿 Codex），
        出错逐级降级并行度直到串行。返回各段摘要与结果引用。
        """
        from concurrent.futures import ThreadPoolExecutor
        from contextvars import copy_context

        from agentTest.config.planner import MAX_QUERY_PARALLEL
        from agentTest.langgraph_app.services.result_store import save_query_script

        # 1. 收集执行单元（保持原始顺序，每段独立 request_id 保证落盘唯一）
        units = []
        for _idx, _step in enumerate(steps_list):
            _step_id = re.sub(r"[^0-9A-Za-z_]", "_", str(_step.get("id") or f"s{_idx + 1}")) or f"s{_idx + 1}"
            _s_sql = str(_step.get("sql") or "")
            _s_question = str(_step.get("question") or fallback_question or "")
            _s_result_limit = int(_step.get("result_limit") or 0)
            if not _s_sql.strip():
                continue
            units.append({
                "key": f"{base_request_id}_{_step_id}",
                "step_id": _step_id,
                "sql": _s_sql,
                "question": _s_question,
                "result_limit": _s_result_limit,
            })
        if not units:
            return "steps 中未找到有效的 SQL 段，请检查每段是否包含 sql 字段。"

        def _exec_unit(_u):
            # 用 copy_context 传播主线程的日志/会话 ContextVar，保证日志归属与落盘正确
            _ctx = copy_context()
            return _ctx.run(
                _exec_one_sql, runtime, _u["sql"], _u["question"], _u["key"],
                step_id=_u["step_id"], result_limit=_u["result_limit"],
            )

        def _is_failed(_rs):
            # 失败判定：执行失败 / 返回 None（_rs 为解包后的 result_state）
            if _rs is None:
                return True
            return bool((_rs or {}).get("sql_exec_failed"))

        # 2. 并行执行，出错逐级降级并行度（如 4→2→1），降级后仅重跑失败单元
        results = {}
        failed_keys = [_u["key"] for _u in units]
        _parallel = MAX_QUERY_PARALLEL
        while failed_keys:
            _todo = [_u for _u in units if _u["key"] in failed_keys]
            if len(_todo) == 1:
                _parallel = 1
            if _parallel <= 1:
                # 串行兜底：逐个执行（保证顺序与上下文稳定）
                for _u in _todo:
                    results[_u["key"]] = _exec_unit(_u)
                break
            with ThreadPoolExecutor(max_workers=_parallel) as _ex:
                _futs = {_ex.submit(_exec_unit, _u): _u for _u in _todo}
                for _fut, _u in _futs.items():
                    try:
                        results[_u["key"]] = _fut.result()
                    except Exception as _err:
                        results[_u["key"]] = (None, f"查询执行异常：{_err}")
            _next_failed = [_k for _k, (_rs, _txt) in results.items() if _is_failed(_rs)]
            if not _next_failed:
                break
            # 并行度减半重试失败单元（可能是并发压力导致，降级后重试）
            _half = _parallel // 2
            if _half < 1:
                break
            log_sub_info(
                f"多段查询并行执行有 {len(_next_failed)} 段失败，降级并行度 {_parallel} -> {_half} 重试",
                node_name="execute_query",
            )
            _parallel = _half
            failed_keys = _next_failed

        # 3. 按原始顺序组装摘要与脚本元数据
        summaries = []
        step_infos = []
        for _u in units:
            _result_state, _text = results.get(_u["key"], (None, "查询执行失败"))
            _sql_result = (_result_state or {}).get("sql_result") or {}
            step_infos.append({
                "step_id": _u["step_id"],
                "question": _u["question"],
                "generated_sql": _u["sql"],
                "result_id": str((_result_state or {}).get("result_id") or ""),
                "row_count": int(_sql_result.get("row_count") or 0),
                "columns": list(_sql_result.get("columns") or []),
                "full_csv": str((_result_state or {}).get("result_csv") or ""),
            })
            summaries.append(f"[{_u['step_id']}] {_text}")
        # 保存查询脚本元数据（仿 Codex），供审计与后续按段引用中间结果
        save_query_script(_current_conversation_id.get(), base_request_id, step_infos)
        return (
            "已并行执行多段查询（每段结果已落盘，可 query_stored_result 按 result_id/round_no 引用）：\n\n"
            + "\n\n".join(summaries)
        )

    @tool
    def execute_query(sql: str = "", question: str = "", steps: str = "", result_limit: int = 0) -> str:
        """执行一条或多条只读 SQL 并返回结果摘要（列 + 预览行 + 行数 + 全量 CSV 路径 + 实际执行 SQL）。

        Planner 基于语义层 read_metric 确认的口径与来源表字段自行生成 SQL 后调用本工具执行；
        程序只负责安全校验（只读/白名单/LIMIT/分区）与结果落盘，不做任何 SQL 生成。参数：
        - sql：只读 SELECT/WITH 查询（必填，除非用 steps 批量提交）
        - question：查询意图（可选，用于落盘脚本元数据与审计）
        - result_limit：返回行数上限（可选，0=不指定；SQL 缺 LIMIT 时程序按安全兜底追加，
          排名/比较类请给合理规模如 10，不要只取 1 条；用户明确只要 1 条时再给 1）
        - steps：可选，多段查询脚本的 JSON 数组字符串，每段 {id, sql, question, result_limit}；
          程序并行执行每段并各自落盘，出错自动降级串行，返回各段摘要与结果引用
        返回 0 行时请自行判断是过滤值问题（可 probe_values 探查实际取值后改 SQL 重查）还是确实无数据；
        安全校验/执行失败返回具体错误，据此修正 SQL 后重试。
        """
        base_request_id = _current_request_id.get()
        if str(steps or "").strip():
            steps_list = _parse_steps(steps)
            if not steps_list:
                return (
                    "steps 解析失败：需要 JSON 数组字符串，如 "
                    "[{\"id\":\"s1\",\"sql\":\"SELECT COUNT(*) FROM db.tbl\",\"question\":\"昨天新增订单数\"}]"
                )
            return _run_steps(steps_list, question, base_request_id)
        if not str(sql or "").strip():
            return "参数错误：sql 不能为空（或用 steps 批量提交多段 SQL）。"
        result_state, text = _exec_one_sql(
            runtime, sql, question, base_request_id, result_limit=result_limit,
        )
        return text

    return execute_query