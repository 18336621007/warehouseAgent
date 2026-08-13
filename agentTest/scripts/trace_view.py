#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 树形 trace 查看工具：读取 langgraph_app.jsonl，按请求渲染调用链
# 用法：
#   python scripts/trace_view.py list [--limit 20] [--date YYYY-MM-DD]
#   python scripts/trace_view.py show <trace_id前缀> [--full] [--state] [--date YYYY-MM-DD]
#   python scripts/trace_view.py prompt <request_id前缀> [--caller C] [--full] [--date YYYY-MM-DD]
#   python scripts/trace_view.py summary <request_id前缀> [--date YYYY-MM-DD]
#   python scripts/trace_view.py slow [--top 10] [--date YYYY-MM-DD]
#   python scripts/trace_view.py nodeslow [--top 20] [--date YYYY-MM-DD]
#   python scripts/trace_view.py tail [--lines 20] [--follow] [--date YYYY-MM-DD]
#   python scripts/trace_view.py filter [--event E] [--node N] [--request R] [--topic T] [--error E] [--keyword K] [--category C]
#   show --state 只展示各 Agent 节点 State 快照；prompt 回放 LLM 输入；summary 查看请求级摘要
import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentTest.langgraph_app.runtime.graph_logger import LOG_DIR, LOG_FILE

# 渲染时省略的通用字段
COMMON_KEYS = {
    "timestamp", "level", "event", "seq", "span_id", "parent_span_id",
    "trace_id", "node", "conversation_id", "topic_id", "request_id",
    "graph_thread_id",
}

# 每种事件优先展示的字段顺序
KEY_BY_EVENT = {
    "request.started": ["input"],
    "request.completed": ["result_type", "route", "topic_status", "node_count", "ms", "summary"],
    "request.failed": ["error_code", "error_message", "ms"],
    "node.started": ["question", "retry", "sql_valid", "sql"],
    "node.completed": ["route", "completeness", "locked", "valid", "retries", "turns", "ms"],
    "node.failed": ["error", "error_message", "ms"],
    "node.degraded": ["error_message", "ms"],
    "node.event": ["message"],
    "node.detail": ["message"],
    "route.decided": ["decision", "completeness", "plan_status", "has_confirmed_plan"],
    "state.changed": ["field", "previous", "current"],
    "tools.called": ["tools"],
    "example.retrieved": ["hit_count", "top_sim", "top_question", "hint"],
    "plan.locked": ["table", "measures", "dimensions", "order_by", "result_limit"],
    "advisor.mode": ["mode", "completeness"],
    "metric_ambiguity.detected": ["mention", "candidate_count"],
    "metric_resolution.user_required": ["mentions"],
    "metric_resolution.completed": ["mention", "field", "source"],
    "cli.round.started": ["round_num"],
    "request.user_input": ["message"],
    "state.snapshot": ["state"],
    "llm.call": ["model", "prompt"],
    "llm.response": ["model", "output", "ms", "tokens"],
    "llm.error": ["model", "error_message", "ms"],
    "candidate_recall": ["mention", "table_scope_count", "raw_candidates", "ranked_candidates"],
    "candidate_rerank.selected": ["mention", "selected_fields"],
    "search.scores": ["layer", "scores"],
}


# 字段中文标签：输出时把英文 key 替换为直观中文，避免满屏英文
KEY_LABELS = {
    "input": "输入",
    "result_type": "结果类型",
    "result": "结果",
    "route": "路由",
    "topic_status": "话题状态",
    "node_count": "节点数",
    "event_count": "事件数",
    "ms": "耗时",
    "summary": "摘要",
    "error_code": "错误码",
    "error_id": "错误编号",
    "error_message": "错误信息",
    "error_type": "错误类型",
    "error": "错误",
    "question": "问题",
    "retry": "重试",
    "sql_valid": "SQL校验",
    "sql": "SQL",
    "completeness": "完整度",
    "locked": "已锁定",
    "valid": "校验通过",
    "retries": "重试次数",
    "turns": "轮次",
    "message": "信息",
    "decision": "决策",
    "plan_status": "方案状态",
    "has_confirmed_plan": "已有方案",
    "field": "字段",
    "previous": "原值",
    "current": "新值",
    "tools": "工具",
    "hit_count": "命中数",
    "top_sim": "最高相似度",
    "top_question": "相似问题",
    "hint": "提示",
    "table": "表",
    "measures": "指标",
    "dimensions": "维度",
    "order_by": "排序",
    "result_limit": "结果上限",
    "mode": "模式",
    "mention": "概念",
    "mentions": "概念列表",
    "candidate_count": "候选数",
    "candidates": "候选",
    "raw_candidates": "召回候选",
    "ranked_candidates": "排序候选",
    "table_scope_count": "表范围数",
    "selected_fields": "已选字段",
    "selected_field": "已选字段",
    "reasoning": "理由",
    "layer": "层级",
    "scores": "评分",
    "state": "状态快照",
    "model": "模型",
    "prompt": "Prompt",
    "output": "输出",
    "tokens": "Tokens",
    "answer_summary": "回答摘要",
    "category": "类别",
    "round_num": "轮数",
    "source": "解析来源",
    "status": "状态",
    "concept_type": "概念类型",
    "node": "节点",
}


def _label(key):
    # 字段名与中文标签并存：route(路由)=advisor
    label = KEY_LABELS.get(key, "")
    return f"{key}({label})" if label else key

_ANSI = {
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


def colorize(text, color_name, enabled):
    if not enabled or not text:
        return text
    return _ANSI[color_name] + text + _ANSI["reset"]


def resolve_log_path(date):
    if date:
        candidate = LOG_DIR / f"{LOG_FILE.name}.{date}"
        if candidate.exists():
            return candidate
    if LOG_FILE.exists():
        return LOG_FILE
    return LOG_FILE


def load_events(path):
    events = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        events.append(obj)
    return events


def _truncate(value, limit):
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _fmt_value(value, limit):
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return _truncate(value, limit)


def summarize(ev, full):
    event = ev.get("event", "")
    keys = KEY_BY_EVENT.get(event, [])
    parts = []
    for key in keys:
        if key not in ev or ev[key] in (None, "", []):
            continue
        limit = 120 if full else 80
        parts.append(f"{_label(key)}={_fmt_value(ev[key], limit)}")
    for key, value in ev.items():
        if key in COMMON_KEYS or key in keys:
            continue
        if value in (None, "", []):
            continue
        limit = 100 if full else 60
        parts.append(f"{_label(key)}={_fmt_value(value, limit)}")
    return " ".join(parts)


def group_by_trace(events):
    traces = {}
    for ev in events:
        trace_id = ev.get("trace_id") or ev.get("request_id") or ""
        if not trace_id:
            continue
        traces.setdefault(trace_id, []).append(ev)
    return traces


def _duration(evs):
    for ev in evs:
        if ev.get("event") == "request.completed" and ev.get("ms") is not None:
            return ev["ms"]
    times = []
    for ev in evs:
        try:
            times.append(datetime.fromisoformat(ev["timestamp"]))
        except Exception:
            continue
    if len(times) >= 2:
        return round((max(times) - min(times)).total_seconds() * 1000, 1)
    return None


def _span_ids(events):
    return {ev.get("span_id") for ev in events if ev.get("span_id")}


SPAN_START_EVENTS = {"request.started", "node.started"}
SPAN_END_EVENTS = {
    "request.completed", "request.failed",
    "node.completed", "node.failed", "node.degraded",
}


def _is_span_event(ev):
    return ev.get("event") in SPAN_START_EVENTS or ev.get("event") in SPAN_END_EVENTS


def _fix_parent(ev, span_ids, nodes):
    # 归属修正：detail/event 等 leaf 事件即使写在节点结束后，也归到同名节点 span 下
    if not _is_span_event(ev):
        node = ev.get("node")
        if node:
            candidates = [
                span_id
                for span_id in span_ids
                if span_id.startswith(f"{node}#")
            ]
            if candidates:
                return max(
                    candidates,
                    key=lambda span_id: nodes[span_id].get("seq", 0),
                )
    parent = ev.get("parent_span_id")
    return parent if parent in span_ids else ""


def build_children(events):
    span_ids = _span_ids(events)
    nodes = {}
    for ev in events:
        span_id = ev.get("span_id")
        if span_id and span_id not in nodes:
            nodes[span_id] = ev
    children = {}
    roots = []
    for ev in events:
        span_id = ev.get("span_id")
        if ev.get("event") in SPAN_END_EVENTS and span_id and span_id in span_ids:
            # 组内完成事件归到自身 span 组，用于合并显示耗时与结果
            children.setdefault(span_id, []).append(ev)
            continue
        parent = _fix_parent(ev, span_ids, nodes)
        if parent:
            children.setdefault(parent, []).append(ev)
        else:
            roots.append(ev)
    return nodes, children, roots


def _group_label(head_ev, completed, full, color):
    event = head_ev.get("event", "")
    if event == "request.started":
        label = colorize("request", "cyan", color)
    else:
        label = head_ev.get("node") or event
    parts = []
    if completed:
        done = completed[0]
        ms = done.get("ms")
        if ms is not None:
            parts.append(colorize(f"[{ms}ms]", "dim", color))
        for key in ("route", "completeness", "locked", "valid", "retries", "turns", "result_type"):
            if done.get(key) not in (None, "", []):
                parts.append(f"{_label(key)}={done[key]}")
    tail = summarize(head_ev, full)
    if tail:
        parts.append(tail)
    return " ".join([label] + parts)


def _leaf_text(ev, full, color):
    event = ev.get("event", "")
    if event in ("node.failed", "request.failed"):
        event_show = colorize(event, "red", color)
    elif event in ("node.degraded",):
        event_show = colorize(event, "yellow", color)
    elif event in ("plan.locked", "metric_resolution.completed"):
        event_show = colorize(event, "green", color)
    else:
        event_show = event
    tail = summarize(ev, full)
    return " ".join([event_show, tail]) if tail else event_show


def _walk(span_id, prefix, is_last, nodes, children, full, color, lines):
    head_ev = nodes[span_id]
    kids = sorted(children.get(span_id, []), key=lambda e: e.get("seq", 0))
    completed = [
        e for e in kids
        if e.get("event") in SPAN_END_EVENTS
    ]
    sub_starts = [e for e in kids if e.get("event") in SPAN_START_EVENTS]
    leaves = [e for e in kids if e not in completed and e not in sub_starts]

    branch = "└─ " if is_last else "├─ "
    lines.append(prefix + branch + _group_label(head_ev, completed, full, color))

    child_prefix = prefix + ("   " if is_last else "│  ")
    for index, leaf in enumerate(leaves):
        last = (index == len(leaves) - 1) and not sub_starts
        leaf_branch = "└─ " if last else "├─ "
        lines.append(child_prefix + leaf_branch + _leaf_text(leaf, full, color))

    for index, sub in enumerate(sub_starts):
        last = index == len(sub_starts) - 1
        _walk(sub.get("span_id"), child_prefix, last, nodes, children, full, color, lines)


def render_trace(trace_id, evs, full, color):
    evs = sorted(evs, key=lambda e: e.get("seq", 0))
    nodes, children, roots = build_children(evs)
    start_time = evs[0].get("timestamp", "")[:19] if evs else ""
    duration = _duration(evs)
    result = next(
        (e.get("result_type", "") for e in evs if e.get("event") == "request.completed"),
        "",
    )
    header = f"==== trace {trace_id}  {start_time}"
    if duration is not None:
        header += f"  [{duration}ms]"
    if result:
        header += f"  result={result}"
    print(header)

    lines = []
    for index, root in enumerate(sorted(roots, key=lambda e: e.get("seq", 0))):
        span_id = root.get("span_id")
        if span_id and span_id in children:
            _walk(span_id, "", index == len(roots) - 1, nodes, children, full, color, lines)
        else:
            lines.append(_leaf_text(root, full, color))
    print("\n".join(lines))


def cmd_list(events, limit, color):
    traces = group_by_trace(events)
    rows = []
    for trace_id, evs in traces.items():
        start_ev = min(evs, key=lambda e: e.get("seq", 0))
        duration = _duration(evs)
        node_count = len({e.get("node") for e in evs if e.get("node")})
        result = next(
            (e.get("result_type", "") for e in evs if e.get("event") == "request.completed"),
            "",
        )
        route = next(
            (e.get("decision", "") for e in evs if e.get("event") == "route.decided"),
            "",
        )
        locked = any(
            e.get("locked") for e in evs if e.get("event") == "node.completed"
        )
        input_text = next(
            (e.get("input", "") for e in evs if e.get("event") == "request.started"),
            "",
        )
        rows.append((
            start_ev.get("timestamp", ""),
            duration,
            trace_id,
            result,
            route,
            node_count,
            len(evs),
            locked,
            input_text,
        ))
    rows.sort(key=lambda row: row[0], reverse=True)
    for timestamp, duration, trace_id, result, route, node_count, event_count, locked, input_text in rows[:limit]:
        line = (
            f"{timestamp[:19]}  {trace_id[:18]:18s}  "
            f"{'----' if duration is None else str(duration) + 'ms':>9s}  "
            f"{_label('node_count')}={node_count:<3d} {_label('event_count')}={event_count:<3d} "
            f"{_label('result')}={result or '-':<8s} {_label('route')}={route or '-'}"
            f"  {_label('input')}={_truncate(input_text, 24)}"
        )
        if locked:
            line += colorize(" locked", "green", color)
        print(line)


def cmd_slow(events, top):
    rows = []
    for trace_id, evs in group_by_trace(events).items():
        duration = _duration(evs)
        if duration is None:
            continue
        start_ev = min(evs, key=lambda e: e.get("seq", 0))
        result = next(
            (e.get("result_type", "") for e in evs if e.get("event") == "request.completed"),
            "",
        )
        rows.append((duration, trace_id, start_ev.get("timestamp", ""), result))
    rows.sort(key=lambda row: row[0], reverse=True)
    for index, (duration, trace_id, timestamp, result) in enumerate(rows[:top], 1):
        print(
            f"{index:>3d}. {duration:>10.1f}ms  {trace_id[:18]:18s}  "
            f"{timestamp[:19]}  {_label('result')}={result or '-'}"
        )


def cmd_filter(events, args, color):
    matches = []
    for ev in events:
        if args.event and ev.get("event") != args.event:
            continue
        if args.category and ev.get("category") != args.category:
            continue
        if args.node and ev.get("node") != args.node:
            continue
        if args.request and not (
            (ev.get("request_id") or ev.get("trace_id") or "").startswith(args.request)
        ):
            continue
        if args.topic and ev.get("topic_id") != args.topic:
            continue
        if args.error and ev.get("error_id") != args.error:
            continue
        if args.keyword:
            joined = json.dumps(ev, ensure_ascii=False)
            if args.keyword not in joined:
                continue
        matches.append(ev)
    matches.sort(key=lambda e: e.get("timestamp", ""))
    for ev in matches[: args.limit]:
        text = _leaf_text(ev, args.full, color)
        print(f"{ev.get('timestamp', '')[:23]}  {text}")


def cmd_tail(path, lines, follow, color):
    # 打印原始日志行，--follow 时持续跟踪新增内容
    position = 0
    if not follow:
        all_lines = io.open(path, encoding="utf-8").read().splitlines()
        for line in all_lines[-lines:]:
            print(line)
        return
    import time
    while True:
        with io.open(path, encoding="utf-8") as f:
            f.seek(position)
            new_lines = f.readlines()
            position = f.tell()
        for line in new_lines:
            if line.strip():
                print(line.rstrip())
        time.sleep(1)


def cmd_nodeslow(events, top, color):
    # 节点级耗时排行，快速定位最慢节点
    rows = []
    for ev in events:
        if ev.get("event") == "node.completed" and ev.get("ms") is not None:
            rows.append((
                ev["ms"],
                ev.get("timestamp", ""),
                ev.get("request_id") or ev.get("trace_id", ""),
                ev.get("node", ""),
            ))
    rows.sort(key=lambda row: row[0], reverse=True)
    for index, (ms, timestamp, request_id, node) in enumerate(rows[:top], 1):
        print(
            f"{index:>3d}. {ms:>10.1f}ms  {node:<20s}  "
            f"{request_id[:16]:16s}  {timestamp[:19]}"
        )




def _print_llm_input(ev, last_prompt, full):
    # 打印单次 LLM 输入；同一调用方连续调用时，首次显示完整，后续仅显示新增消息
    ts = ev.get("timestamp", "")[:23]
    node = ev.get("node", "?")
    model = ev.get("model", "")
    print(f"==== {ts}  [{node}]  model={model}")
    prompt = ev.get("prompt", "") or ""
    prev = last_prompt.get(node, "")
    if prev and prompt == prev:
        print("【输入】")
        print("（与上次调用相同，省略）")
    elif prev and prompt.startswith(prev):
        print("【输入（新增部分）】")
        body = prompt[len(prev):].strip("\n")
        if not full and len(body) > 1200:
            body = body[:1200] + "\n...(截断，使用 --full 查看完整输入)"
        print(body if body else "（无新增内容）")
    else:
        print("【输入】")
        if not full and len(prompt) > 1200:
            prompt = prompt[:1200] + "\n...(截断，使用 --full 查看完整输入)"
        print(prompt)
    last_prompt[node] = prompt


def _print_llm_output(ev, full):
    # 打印单次 LLM 输出（含耗时与 tokens）
    output = ev.get("output", "") or ""
    print("【输出】")
    if not full and len(output) > 1200:
        output = output[:1200] + "\n...(截断，使用 --full 查看完整输出)"
    print(output)
    ms = ev.get("ms")
    tokens = ev.get("tokens") or {}
    if ms is not None or tokens:
        print(f"(耗时: {ms}ms  tokens: {tokens})")
    print()


def cmd_prompt(events, prefix, caller, full, color):
    # 按时间戳顺序，用 call_id 配对“调用→输出”；
    # 避免多线程下 seq 错乱导致输入/输出分别聚在一起
    matched = 0
    pending = {}
    last_prompt = {}
    for ev in sorted(events, key=lambda e: e.get("timestamp", "")):
        if ev.get("event") not in ("llm.call", "llm.response"):
            continue
        if not (ev.get("request_id") or ev.get("trace_id") or "").startswith(prefix):
            continue
        if caller and ev.get("node") != caller:
            continue
        call_id = ev.get("call_id", "")
        if ev.get("event") == "llm.call":
            if call_id:
                pending[call_id] = ev
            else:
                # 无 call_id 的旧日志：按顺序直接打印
                _print_llm_input(ev, last_prompt, full)
                matched += 1
        else:
            call_ev = pending.pop(call_id, None) if call_id else None
            if call_ev is not None:
                _print_llm_input(call_ev, last_prompt, full)
                matched += 1
            _print_llm_output(ev, full)
    if matched == 0:
        print(
            f"未找到 llm.call 事件: request={prefix} caller={caller or '任意'}",
            file=sys.stderr,
        )


def cmd_summary(events, prefix, color):
    # 请求级摘要：输入/结果/耗时、LLM 调用分布与事件分布
    from collections import Counter
    req_events = [
        ev for ev in events
        if (ev.get("request_id") or ev.get("trace_id") or "").startswith(prefix)
    ]
    if not req_events:
        print(f"未找到请求: {prefix}", file=sys.stderr)
        sys.exit(1)
    start = next(
        (ev for ev in req_events if ev.get("event") == "request.started"),
        None,
    )
    end = next(
        (ev for ev in req_events if ev.get("event") == "request.completed"),
        None,
    )
    trace_id = req_events[0].get("request_id") or req_events[0].get("trace_id") or ""
    ts = (start or req_events[0]).get("timestamp", "")[:19]
    print(f"==== 请求摘要 {trace_id}  {ts}")
    if start:
        print(f"输入: {_truncate(start.get('input', ''), 120)}")
    if end:
        summary = end.get("summary") or {}
        ms = end.get("ms")
        print(
            f"结果: {end.get('result_type', '-')}  耗时: {ms if ms is not None else '-'}ms  "
            f"节点数: {summary.get('nodes', end.get('node_count', '-'))}  "
            f"LLM 调用: {summary.get('llm_calls', '-')}  "
            f"route: {end.get('route', '-')}  topic_status: {end.get('topic_status', '-')}"
        )
        extra = {
            key: value
            for key, value in summary.items()
            if key not in ("nodes", "llm_calls", "route", "topic_status")
        }
        if extra:
            print(f"其他摘要: {json.dumps(extra, ensure_ascii=False)}")
    llm_calls = [ev for ev in req_events if ev.get("event") == "llm.call"]
    if llm_calls:
        print("\nLLM 调用分布:")
        for caller_name, count in Counter(
            ev.get("node", "?") for ev in llm_calls
        ).most_common():
            print(f"  {caller_name}: {count}")
    print("\n事件分布:")
    for event_name, count in Counter(
        ev.get("event", "?") for ev in req_events
    ).most_common():
        print(f"  {event_name}: {count}")
    print("\n节点分布:")
    for node_name, count in Counter(
        ev.get("node", "?") for ev in req_events if ev.get("node")
    ).most_common():
        print(f"  {node_name}: {count}")


def cmd_state(events, prefix, color):
    # 按执行顺序展示各 Agent 节点完成后的 State 快照
    snapshots = [
        ev for ev in events
        if (ev.get("request_id") or ev.get("trace_id") or "").startswith(prefix)
        and ev.get("event") == "state.snapshot"
    ]
    if not snapshots:
        print(f"未找到 state.snapshot 事件: {prefix}", file=sys.stderr)
        sys.exit(1)
    for ev in sorted(snapshots, key=lambda e: e.get("seq", 0)):
        ts = ev.get("timestamp", "")[:23]
        print(f"==== {ts}  [{ev.get('node', '?')}]")
        print(json.dumps(ev.get("state") or {}, ensure_ascii=False, indent=2))
        print()


def main():
    parser = argparse.ArgumentParser(description="langgraph_app.jsonl 树形 trace 查看工具")
    parser.add_argument("--date", default="", help="日志日期 YYYY-MM-DD，默认读取当前文件")
    parser.add_argument("--no-color", action="store_true", help="禁用彩色输出")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="列出最近请求")
    list_parser.add_argument("--limit", type=int, default=20)

    show_parser = subparsers.add_parser("show", help="渲染单个请求的树形 trace")
    show_parser.add_argument("trace_id", help="trace_id 或 request_id 前缀")
    show_parser.add_argument("--full", action="store_true", help="不截断长文本")
    show_parser.add_argument("--state", action="store_true", help="只展示各节点 State 快照")

    slow_parser = subparsers.add_parser("slow", help="按请求耗时排行")
    slow_parser.add_argument("--top", type=int, default=10)

    filter_parser = subparsers.add_parser("filter", help="按事件/节点/请求/关键词过滤")
    filter_parser.add_argument("--event", default="")
    filter_parser.add_argument("--node", default="")
    filter_parser.add_argument("--request", default="", help="request_id 前缀")
    filter_parser.add_argument("--topic", default="", help="topic_id")
    filter_parser.add_argument("--error", default="", help="error_id")
    filter_parser.add_argument("--keyword", default="")
    filter_parser.add_argument("--category", default="", help="lifecycle/state/llm/metric/search/plan")
    filter_parser.add_argument("--limit", type=int, default=50)
    filter_parser.add_argument("--full", action="store_true")

    tail_parser = subparsers.add_parser("tail", help="查看原始日志尾部")
    tail_parser.add_argument("--lines", type=int, default=20)
    tail_parser.add_argument("--follow", action="store_true", help="持续跟踪新增内容")

    nodeslow_parser = subparsers.add_parser("nodeslow", help="节点耗时排行")
    nodeslow_parser.add_argument("--top", type=int, default=20)

    prompt_parser = subparsers.add_parser("prompt", help="查看指定请求的 LLM 输入 prompt")
    prompt_parser.add_argument("request", help="request_id 前缀")
    prompt_parser.add_argument("--caller", default="", help="只显示指定调用方（如 planner/advisor/generate_sql）")
    prompt_parser.add_argument("--full", action="store_true", help="不截断 prompt")

    summary_parser = subparsers.add_parser("summary", help="请求级摘要（节点/LLM/事件分布）")
    summary_parser.add_argument("request", help="request_id 前缀")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    log_path = resolve_log_path(args.date)
    if not log_path.exists():
        print(f"日志文件不存在: {log_path}", file=sys.stderr)
        sys.exit(1)
    events = load_events(log_path)
    color = sys.stdout.isatty() and not args.no_color

    if args.command == "list":
        cmd_list(events, args.limit, color)
    elif args.command == "show":
        traces = group_by_trace(events)
        matched = [
            (trace_id, evs)
            for trace_id, evs in traces.items()
            if trace_id.startswith(args.trace_id)
        ]
        if not matched:
            print(f"未找到 trace: {args.trace_id}", file=sys.stderr)
            sys.exit(1)
        if len(matched) > 1:
            print(f"匹配到多个 trace，请使用更长的前缀：{args.trace_id}")
            for trace_id, _ in matched:
                print("  ", trace_id)
            sys.exit(1)
        if args.state:
            cmd_state(events, args.trace_id, color)
        else:
            render_trace(matched[0][0], matched[0][1], args.full, color)
    elif args.command == "slow":
        cmd_slow(events, args.top)
    elif args.command == "filter":
        cmd_filter(events, args, color)
    elif args.command == "tail":
        cmd_tail(log_path, args.lines, args.follow, color)
    elif args.command == "nodeslow":
        cmd_nodeslow(events, args.top, color)
    elif args.command == "prompt":
        cmd_prompt(events, args.request, args.caller, args.full, color)
    elif args.command == "summary":
        cmd_summary(events, args.request, color)


if __name__ == "__main__":
    main()
