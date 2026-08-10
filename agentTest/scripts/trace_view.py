#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 树形 trace 查看工具：读取 langgraph_app.jsonl，按请求渲染调用链
# 用法：
#   python scripts/trace_view.py list [--limit 20] [--date YYYY-MM-DD]
#   python scripts/trace_view.py show <trace_id前缀> [--full] [--date YYYY-MM-DD]
#   python scripts/trace_view.py slow [--top 10] [--date YYYY-MM-DD]
#   python scripts/trace_view.py nodeslow [--top 20] [--date YYYY-MM-DD]
#   python scripts/trace_view.py tail [--lines 20] [--follow] [--date YYYY-MM-DD]
#   python scripts/trace_view.py filter [--event E] [--node N] [--request R] [--topic T] [--error E] [--keyword K]
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
    "request.completed": ["result_type", "route", "topic_status", "node_count", "ms"],
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
}

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
        parts.append(f"{key}={_fmt_value(ev[key], limit)}")
    for key, value in ev.items():
        if key in COMMON_KEYS or key in keys:
            continue
        if value in (None, "", []):
            continue
        limit = 100 if full else 60
        parts.append(f"{key}={_fmt_value(value, limit)}")
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
                parts.append(f"{key}={done[key]}")
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
        rows.append((
            start_ev.get("timestamp", ""),
            duration,
            trace_id,
            result,
            route,
            node_count,
            len(evs),
            locked,
        ))
    rows.sort(key=lambda row: row[0], reverse=True)
    for timestamp, duration, trace_id, result, route, node_count, event_count, locked in rows[:limit]:
        line = (
            f"{timestamp[:19]}  {trace_id[:18]:18s}  "
            f"{'----' if duration is None else str(duration) + 'ms':>9s}  "
            f"nodes={node_count:<3d} evts={event_count:<3d} result={result or '-':<8s} route={route or '-'}"
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
            f"{timestamp[:19]}  result={result or '-'}"
        )


def cmd_filter(events, args, color):
    matches = []
    for ev in events:
        if args.event and ev.get("event") != args.event:
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

    slow_parser = subparsers.add_parser("slow", help="按请求耗时排行")
    slow_parser.add_argument("--top", type=int, default=10)

    filter_parser = subparsers.add_parser("filter", help="按事件/节点/请求/关键词过滤")
    filter_parser.add_argument("--event", default="")
    filter_parser.add_argument("--node", default="")
    filter_parser.add_argument("--request", default="", help="request_id 前缀")
    filter_parser.add_argument("--topic", default="", help="topic_id")
    filter_parser.add_argument("--error", default="", help="error_id")
    filter_parser.add_argument("--keyword", default="")
    filter_parser.add_argument("--limit", type=int, default=50)
    filter_parser.add_argument("--full", action="store_true")

    tail_parser = subparsers.add_parser("tail", help="查看原始日志尾部")
    tail_parser.add_argument("--lines", type=int, default=20)
    tail_parser.add_argument("--follow", action="store_true", help="持续跟踪新增内容")

    nodeslow_parser = subparsers.add_parser("nodeslow", help="节点耗时排行")
    nodeslow_parser.add_argument("--top", type=int, default=20)

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
        render_trace(matched[0][0], matched[0][1], args.full, color)
    elif args.command == "slow":
        cmd_slow(events, args.top)
    elif args.command == "filter":
        cmd_filter(events, args, color)
    elif args.command == "tail":
        cmd_tail(log_path, args.lines, args.follow, color)
    elif args.command == "nodeslow":
        cmd_nodeslow(events, args.top, color)


if __name__ == "__main__":
    main()
