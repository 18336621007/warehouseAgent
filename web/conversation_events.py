# conversation_events.py —— 会话列表 SSE 广播器
# 前端首屏只读一次全量，之后订阅本模块推送的变更，替代 2.5s 轮询 /api/conversations。
# sessions 由 server 启动后注入同一内存字典，飞书等后台源复用同一实例。
import json
import queue
import threading

from web.active_requests import ACTIVE_REQUESTS

_SUBSCRIBERS = set()
_LOCK = threading.Lock()
_SESSIONS_REF = {}


def set_sessions(sessions) -> None:
    """注入会话内存字典（server.py 启动加载 MySQL 后调用，与 feishu_bot 共用同一实例）。"""
    _SESSIONS_REF["sessions"] = sessions


def build_conversation_snapshot() -> list:
    """生成与 GET /api/conversations 一致的会话列表快照（仅轻量元数据，不含完整消息）。"""
    sessions = _SESSIONS_REF.get("sessions") or {}
    convs = []
    for conversation_id, sess in sessions.items():
        messages = sess.get("messages") or []
        first = sess.get("title_override") or (messages[0]["content"] if messages else "New Chat")
        last_msg = messages[-1] if messages else None
        convs.append({
            "conversation_id": conversation_id,
            "title": first[:50],
            "message_count": len(messages),
            "creator": sess.get("creator", ""),
            "active": conversation_id in ACTIVE_REQUESTS,
            "last_status": (last_msg or {}).get("status", ""),
        })
    return convs


def _sse_payload() -> str:
    return "data: " + json.dumps({"conversations": build_conversation_snapshot()}, ensure_ascii=False) + "\n\n"


def broadcast_conversations() -> None:
    """推送最新会话列表快照给所有订阅者：每个订阅者只保留最新一次，消费慢时自动丢弃旧快照。"""
    payload = _sse_payload()
    with _LOCK:
        for q in list(_SUBSCRIBERS):
            try:
                q.get_nowait()
            except Exception:
                pass
            try:
                q.put_nowait(payload)
            except (queue.Full, Exception):
                pass


def subscribe() -> queue.Queue:
    """注册 SSE 订阅者，返回容量为 1 的事件队列。"""
    q = queue.Queue(maxsize=1)
    with _LOCK:
        _SUBSCRIBERS.add(q)
    return q


def unsubscribe(q) -> None:
    """取消订阅（SSE 连接关闭/异常时清理，防止泄露）。"""
    with _LOCK:
        _SUBSCRIBERS.discard(q)


def initial_sse_payload() -> str:
    """连接建立后立即下发一次当前快照，前端无需再额外轮询。"""
    return _sse_payload()
