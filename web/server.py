"""
ChatGPT UI backend - Flask API (streaming + scoring + rename/delete)
"""
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
import uuid, os, sys, json, threading, queue, time, socket, re
from datetime import datetime

# 强制 stdout/stderr 行缓冲，让启动日志与请求日志实时显示（避免块缓冲憋住，进程结束才刷出）
sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentTest.langgraph_app.runtime.graph_logger import bind_log_context
from agentTest.langgraph_app.runtime.graph_logger import reset_log_context
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_degraded
from agentTest.langgraph_app.runtime.graph_logger import log_request_end
from agentTest.langgraph_app.runtime.graph_logger import log_request_error
from agentTest.langgraph_app.runtime.graph_logger import log_request_start
from agentTest.langgraph_app.runtime.graph_logger import log_state_change
from agentTest.langgraph_app.runtime.graph_logger import get_llm_token_usage
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.graphs.supervisor_graph import build_supervisor_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime
from agentTest.langgraph_app.runtime.stream_bus import StreamBus, bind_stream_bus
from web.active_requests import ACTIVE_REQUESTS, CANCEL_FLAGS, NODE_LABELS, _SEEKER_INTERNAL_NODES, _extract_node_detail, clear_cancel_flag, is_cancel_requested, make_active_sink, set_cancel_flag
from web.conversation_events import broadcast_conversations, build_conversation_snapshot, initial_sse_payload, set_sessions, subscribe, unsubscribe
from agentTest.langgraph_app.nodes.planner_node import PLANNER_SYSTEM_PROMPT
from agentTest.langgraph_app.nodes.planner_node import _build_history_context
from agentTest.langgraph_app.nodes.planner_node import _estimate_tokens
from agentTest.config.settings import get_stream_output_enabled
from agentTest.config.settings import get_model_context_window
from web.intent_classifier import classify_intent
from web.conversation_store import init_db as init_conv_db
from web.conversation_store import load_all as load_conv_all
from web.conversation_store import upsert as upsert_conv
from web.conversation_store import soft_delete as soft_delete_conv
from agentTest.metadata.mysql_store import update_user_score
from langchain_core.messages import RemoveMessage

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # 前端静态文件禁用长缓存，改代码后刷新即可生效

print("[server] loading LangGraph runtime...")
RUNTIME = build_graph_runtime()
APP = build_supervisor_graph(RUNTIME)
print("[server] runtime ready")

# 对话记录落盘（MySQL）：启动时恢复全部历史会话，会话变更同步写库（方案 A：共享全部会话）
init_conv_db()
try:
    sessions = load_conv_all()
except Exception as error:
    # MySQL 不可用时以空缓存启动，不影响服务运行（落盘是辅助能力）
    print(f"[server] load conversations failed: {error}")
    sessions = {}

set_sessions(sessions)


# 当前请求的流式总线注册表：用户点停止时由 cancel_chat 立即 close，
# 让 SSE 通道与前端快速收尾，不必等后台 LLM 自然生成完
ACTIVE_BUSES = {}


# 飞书机器人：长连接接收私有/群@消息，复用同一 RUNTIME/APP/sessions；启动失败不阻塞 Web 服务
def _start_feishu_bot():
    try:
        from web.feishu_bot import start_feishu_bot as _start_feishu
        _start_feishu(RUNTIME, APP, sessions, ACTIVE_REQUESTS)
    except Exception as error:
        print(f"[server] 飞书机器人启动失败（不影响 Web 服务）: {error}")


_start_feishu_bot()


def _persist_conversation(conversation_id):
    """落盘会话记录；MySQL 异常仅记日志，不阻塞聊天主流程。"""
    try:
        upsert_conv(conversation_id, sessions[conversation_id])
    except Exception as error:
        print(f"[server] persist conversation failed: {error}")


def _cancel_partial_content(conversation_id, fallback="已停止生成") -> str:
    """停止后读取已流出的部分回答：ACTIVE_REQUESTS 可能已被 cancel_chat 清除，从 CANCEL_FLAGS 兜底。"""
    snap = ACTIVE_REQUESTS.get(conversation_id) or {}
    content = str(snap.get("content") or "").strip()
    if not content:
        flag = CANCEL_FLAGS.get(conversation_id) or {}
        content = str(flag.get("content") or "").strip()
    return content or fallback


def _cancel_and_wait_old_request(conversation_id: str, timeout_seconds: float = 5) -> bool:
    """若该会话仍有旧请求在进行，先请求取消并等待其后台线程收尾。

    避免出现 "conversation busy"：新消息到来时自动停掉旧请求（对齐 Codex 取消整个任务树），
    并且只有等旧 worker 清理完 ACTIVE_REQUESTS 才继续，防止旧 worker 覆盖新一轮消息。
    """
    snap = ACTIVE_REQUESTS.get(conversation_id)
    if not snap:
        return True
    old_rid = str(snap.get("request_id") or "")
    snap["cancel_requested"] = True
    if old_rid:
        set_cancel_flag(conversation_id, old_rid, _cancel_partial_content(conversation_id))
        # 立即把旧轮占位写成 aborted，用户刷新/切回即可看到已停止
        _message_list = sessions.get(conversation_id, {}).get("messages") or []
        _last = _message_list[-1] if _message_list else {}
        if (_last.get("role") == "assistant" and _last.get("status") == "processing"
                and _last.get("request_id") == old_rid):
            _message_list[-1] = {
                "role": "assistant",
                "content": _cancel_partial_content(conversation_id),
                "thinking": str(_last.get("thinking") or ""),
                "sql": "", "dialogue_id": 0, "evaluator": None, "llm_tokens": {},
                "request_id": old_rid, "thinking_seconds": 0,
                "status": "aborted", "error_message": "", "can_chart": False,
                "request_at": _last.get("request_at", ""),
            }
            _persist_conversation(conversation_id)
    # 取消已生效（LLM 流内/SQL 轮询都会很快退出），等旧 worker finally 清掉进行中注册表
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if conversation_id not in ACTIVE_REQUESTS:
            return True
        time.sleep(0.05)
    # 兜底：旧 worker 若因尾部事件重建注册表而未清理，这里按 request_id 强制清掉，
    # 避免后续发消息被 "conversation busy" 拒绝；真正归属由各写回路径 request_id 守卫保护
    if ((ACTIVE_REQUESTS.get(conversation_id) or {}).get("request_id") or "") == old_rid:
        ACTIVE_REQUESTS.pop(conversation_id, None)
        print(f"[server] force cleared stale ACTIVE_REQUESTS for {conversation_id} (rid={old_rid})")
    return True


def _is_current_request(conversation_id: str, request_id: str) -> bool:
    """判断消息末尾是否仍归属指定请求：防止旧 worker 收尾时覆盖最新一轮。"""
    if not request_id:
        return False
    _msg_list = sessions.get(conversation_id, {}).get("messages") or []
    _last = _msg_list[-1] if _msg_list else {}
    return str(_last.get("request_id") or "") == str(request_id)


def _rollback_checkpoint_last_round(app, config, request_id):
    """重新生成时回滚 LangGraph Checkpoint 最近一轮：删除该轮消息并清空本轮残留状态。"""
    if not request_id:
        return
    try:
        snap = app.get_state(config)
        state = (snap and snap.values) or {}
        messages = state.get("messages") or []
        mark = f"{request_id}:user"
        idx = None
        for i in range(len(messages) - 1, -1, -1):
            mid = str(getattr(messages[i], "id", "") or "")
            if mid == mark:
                idx = i
                break
        if idx is None:
            return
        # 用 RemoveMessage 经 add_messages reducer 删除该轮全部消息（user/assistant/ReAct 工具轮）
        removes = [RemoveMessage(id=str(m.id)) for m in messages[idx:] if getattr(m, "id", None)]
        # 重新生成不保留旧轮：相关派生态位一并复位，避免旧方案/旧结果引用泄漏到新查询
        state_updates = {
            "messages": removes,
            "topic_status": "new",
            "effective_query": "",
            "confirmed_plan": {},
            "last_query_result": None,
            "executed_sql": [],
            "planner_reason": "",
            "respond_text": "",
            "route": "execute",
            "seeker_plan_error": None,
            "seeker_error_unresolvable": None,
            "seeker_empty_result": False,
            "empty_result_rounds": 0,
            "self_heal_note": "",
        }
        app.update_state(config, state_updates)
    except Exception as error:
        # 回滚失败不阻塞主流程，但要记录，便于审计重新生成是否真正隔离了旧记忆
        log_node_degraded(
            "regenerate_rollback",
            error,
            error_code="REGENERATE_CHECKPOINT_ROLLBACK_DEGRADED",
            stage="checkpoint_rollback",
        )

def _soft_delete_conversation(conversation_id):
    """软删除会话：仅打 deleted_at 标记保留 MySQL 记录；异常仅记日志。"""
    try:
        soft_delete_conv(conversation_id)
    except Exception as error:
        print(f"[server] soft delete conversation failed: {error}")

def _estimate_conversation_context(conversation_id):
    """按会话实时估算上下文占用：从持久化 Checkpoint 现算（不落库），反映当前真实状态。

    口径与 Planner 组装一致：系统提示 + 技能索引 + 对话历史 + 最近 effective_query；
    工具原文是每轮内存态、不持久化，故稳态占用不含它（在线执行时仍由真实 input_tokens 实时推）。
    """
    try:
        snapshot = APP.get_state({"configurable": {"thread_id": conversation_id}})
        values = (snapshot and snapshot.values) or {}
        messages = values.get("messages") or []
        # 空会话（用户尚未发起任何对话）：上下文占用视为 0，前端圆环显示空
        if not messages:
            return None
        history = _build_history_context(messages)
        last_query = str(values.get("effective_query") or "")
        skill_manager = RUNTIME.get("skill_manager")
        skill_index = (
            skill_manager.list_skills_index(scope="planner", max_chars=4000)
            if skill_manager else ""
        )
        used = (
            _estimate_tokens(PLANNER_SYSTEM_PROMPT)
            + _estimate_tokens(skill_index)
            + _estimate_tokens(history)
            + _estimate_tokens(last_query)
        )
        if not used:
            return None
        window = get_model_context_window()
        return {
            "used_tokens": used,
            "window_tokens": window,
            "percent": round(min(100.0, used * 100.0 / window), 1),
        }
    except Exception:
        return None

# 前端只展示安全错误信息，内部异常通过error_id在日志中定位
QUERY_ERROR_CODE = "QUERY_EXECUTION_FAILED"
QUERY_SAFE_ERROR_MESSAGE = "系统暂时无法完成本次查询，请稍后重试。"

def _sse(data_dict):
    return "data: " + json.dumps(data_dict, ensure_ascii=False) + "\n\n"

@app.before_request
def log_request():
    if request.path.startswith("/api/chat"):
        print(f"[server] {request.method} {request.path} (stream)")

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "sessions": len(sessions)})

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/conversations", methods=["POST"])
def create_conversation():
    # conversation_id对应前端的一个完整对话
    conversation_id = uuid.uuid4().hex

    # 去 Topic 化：conversation 级固定 topic_id（仅作日志/状态标识，不再按问数切换）
    topic_id = uuid.uuid4().hex

    data = request.get_json(silent=True) or {}
    creator = (data.get("creator") or "").strip() or "未命名"
    sessions[conversation_id] = {
        "topic_id": topic_id,
        "creator": creator,
        "messages": [],
    }
    _persist_conversation(conversation_id)
    broadcast_conversations()
    # 产品接口统一使用conversation_id，避免与LangGraph内部thread_id混淆
    return jsonify({
        "conversation_id": conversation_id,
        "creator": creator,
    })

@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    # 首屏只读一次全量；后续变更由 SSE（/api/conversations/events）推送
    return jsonify({"conversations": build_conversation_snapshot()})


@app.route("/api/conversations/events")
def conversation_events():
    """SSE 推送会话列表变更：前端首次只读一次全量，之后订阅本端点接收变更，替代 2.5s 轮询。"""
    q = subscribe()

    def _gen():
        try:
            yield initial_sse_payload()
            while True:
                try:
                    payload = q.get(timeout=30)
                    yield payload
                except queue.Empty:
                    # 心跳保活，避免开发服务器/代理空闲超时断开
                    yield ": keep-alive\n\n"
        finally:
            unsubscribe(q)

    return Response(
        stream_with_context(_gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/conversations/<conversation_id>", methods=["GET"])
def get_conversation(conversation_id):
    """返回单个会话的完整消息，供前端加载（含他人创建的历史）时拉取。"""
    if conversation_id not in sessions: return jsonify({"error": "invalid"}), 400
    sess = sessions[conversation_id]
    return jsonify({
        "conversation_id": conversation_id,
        "creator": sess.get("creator", ""),
        "title": sess.get("title_override") or (sess["messages"][0]["content"] if sess["messages"] else "New Chat"),
        "messages": sess["messages"],
        # 上下文占用实时从 Checkpoint 现算（不同会话各自独立），供前端恢复圆环
        "context": _estimate_conversation_context(conversation_id),
    })

@app.route("/api/conversations/<conversation_id>", methods=["PUT"])
def rename_conversation(conversation_id):
    data = request.get_json()
    title = (data.get("title") or "").strip()
    if conversation_id not in sessions: return jsonify({"error": "invalid"}), 400
    if title: sessions[conversation_id]["title_override"] = title
    _persist_conversation(conversation_id)
    broadcast_conversations()
    return jsonify({"success": True})

@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id):
    # 软删除：仅前端不可见，MySQL 记录保留并打 deleted_at 标记（供审计）
    if conversation_id in sessions:
        del sessions[conversation_id]
    _soft_delete_conversation(conversation_id)
    broadcast_conversations()
    return jsonify({"success": True})

@app.route("/api/chat/status/<conversation_id>")
def chat_status(conversation_id):
    """进行中请求状态（供刷新/切换会话后轮询恢复，仿 codex 的进行中任务可见）。"""
    snap = ACTIVE_REQUESTS.get(conversation_id)
    if not snap:
        return jsonify({"active": False})
    return jsonify({
        "active": True,
        "request_id": snap.get("request_id", ""),
        "status": snap.get("status", ""),
        "thinking": snap.get("thinking", ""),
        "content": snap.get("content", ""),
        "sql": snap.get("sql", ""),
        "context": snap.get("context"),
        "llm_tokens": snap.get("llm_tokens", {}),
        "cancel_requested": bool(snap.get("cancel_requested")),
    })



@app.route("/api/chat/cancel", methods=["POST"])
def cancel_chat():
    """终止指定会话当前正在进行的查询（只服务网页端生成中停止按钮）。

    后台 graph 线程在流式迭代每个 chunk 间检查 cancel_requested，
    命中后中断循环并把本轮消息标记为 aborted（保留已流出的部分回答）。
    """
    data = request.get_json() or {}
    conversation_id = str(data.get("conversation_id") or "")
    request_id = str(data.get("request_id") or "")
    snap = ACTIVE_REQUESTS.get(conversation_id)
    if not snap:
        return jsonify({"success": False, "error": "no active request"}), 200
    if request_id and snap.get("request_id") and request_id != snap.get("request_id"):
        return jsonify({"success": False, "error": "request mismatch"}), 200
    snap["cancel_requested"] = True
    # 先取当前已流出内容再设置标记（ACTIVE_REQUESTS 随后立即清除，供 worker 兜底）
    _partial = _cancel_partial_content(conversation_id)
    set_cancel_flag(conversation_id, request_id, _partial)
    # 立即关闭当前请求的 SSE 总线：前端 abort 后服务器端也马上收尾，不再继续推事件
    _active_bus = ACTIVE_BUSES.get(conversation_id)
    if _active_bus is not None:
        try:
            _active_bus.close()
        except Exception:
            pass
    # 停止立即落盘 aborted 终态：用户刷新/切回会话即可看到，不再等待后台 worker 自然结束
    _cancel_snap = ACTIVE_REQUESTS.get(conversation_id) or {}
    _message_list = sessions.get(conversation_id, {}).get("messages") or []
    _last = _message_list[-1] if _message_list else {}
    if (_last.get("role") == "assistant"
            and _last.get("status") == "processing"
            and _last.get("request_id") == request_id):
        _message_list[-1] = {
            "role": "assistant",
            "content": _partial,
            "thinking": str(_cancel_snap.get("thinking") or ""),
            "sql": "", "dialogue_id": 0, "evaluator": None, "llm_tokens": {},
            "request_id": request_id,
            "thinking_seconds": 0,
            "status": "aborted", "error_message": "", "can_chart": False,
            "request_at": _last.get("request_at", ""),
        }
        _persist_conversation(conversation_id)
    # 从进行中注册表移除：前端停止轮询并立即加载已落盘的 aborted 终态
    if (ACTIVE_REQUESTS.get(conversation_id) or {}).get("request_id") == request_id:
        ACTIVE_REQUESTS.pop(conversation_id, None)
    broadcast_conversations()
    return jsonify({"success": True})


@app.route("/api/chart", methods=["POST"])
def generate_chart():
    """按回答对应的落盘结果生成图表 spec（前端『生成图表』按钮，仿豆包）。

    输入 conversation_id + 该回答的 request_id（含 _pN 分段时按前缀匹配全部落盘结果），
    程序从落盘结果取真实数据并自动探测 x/y 字段，返回规范 chart spec 列表。
    复用 chart_tool 的 build_charts_for_request，与 make_chart 工具同一套取数与格式逻辑。
    """
    data = request.get_json() or {}
    conversation_id = str(data.get("conversation_id") or "")
    request_id = str(data.get("request_id") or "")
    if conversation_id not in sessions:
        return jsonify({"error": "invalid conversation_id"}), 400
    try:
        from agentTest.langgraph_app.tools.chart_tool import build_charts_for_request
        specs, err = build_charts_for_request(
            conversation_id,
            request_id,
            type=str(data.get("type") or "line"),
            x_field=str(data.get("x_field") or ""),
            y_fields=data.get("y_fields") or [],
            title=str(data.get("title") or ""),
        )
    except Exception as error:
        return jsonify({"success": False, "error": f"图表生成失败：{error}"}), 200
    if err:
        return jsonify({"success": False, "error": err}), 200
    return jsonify({"success": True, "charts": specs})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    conversation_id = data.get("conversation_id", "")
    message = data.get("message", "").strip()
    if not conversation_id or conversation_id not in sessions: return jsonify({"error": "invalid conversation_id"}), 400
    if not message: return jsonify({"error": "empty message"}), 400
    # 有旧请求仍在进行时不再直接拒绝：先取消旧请求并等待其收尾，再开启新请求
    if conversation_id in ACTIVE_REQUESTS:
        if not _cancel_and_wait_old_request(conversation_id):
            return jsonify({"error": "conversation busy, please try again later"}), 400

    session = sessions[conversation_id]

    # 重新生成最近一轮：前端编辑最近一条消息后发送，先删除旧 user+assistant 再重建，
    # 不做审计保留（已按需求确认）。正在生成中禁止触发。
    regenerate_old_request_id = ""
    if data.get("regenerate_last"):
        if conversation_id in ACTIVE_REQUESTS:
            if not _cancel_and_wait_old_request(conversation_id):
                return jsonify({"error": "conversation busy, please try again later"}), 400
        latest = session["messages"]
        if (len(latest) >= 2 and latest[-1].get("role") == "assistant"
                and latest[-2].get("role") == "user"
                and latest[-1].get("status") not in ("processing", "chat")):
            # 先记录被编辑轮的 request_id，供 Checkpoint 回滚定位旧轮消息
            regenerate_old_request_id = latest[-1].get("request_id", "")
            del latest[-2:]
            _persist_conversation(conversation_id)

    # 去 Topic 化：整个对话共享一个 Checkpoint，topic_id 固定不再切换
    topic_id = session["topic_id"]

    # 每次HTTP请求使用独立request_id
    request_id = uuid.uuid4().hex

    def _sse_req(data_dict):
        # SSE 事件统一携带 request_id，前端可据此关联日志排查问题
        return _sse({**data_dict, "request_id": request_id})

    # 去 Topic 化：整个对话共享同一个 LangGraph Checkpoint（完整历史跨问数保留）
    graph_thread_id = conversation_id
    config = {
        "configurable": {
            "thread_id": graph_thread_id,
        }
    }
    # 重新生成最近一轮：同步回滚 Checkpoint 旧轮消息与残留状态，避免旧对话污染新查询
    if data.get("regenerate_last"):
        _rollback_checkpoint_last_round(APP, config, regenerate_old_request_id)

    def generate(request_timer, topic_state, request_started_at):
        # ── query: LangGraph pipeline ──
        # 去 Topic 化：是否本对话首轮（Checkpoint 尚无消息），首轮才做意图识别
        is_first_topic_turn = not bool(topic_state.get("messages"))

        # 保存请求开始前的Topic状态，用于识别真实状态变化
        observed_topic_status = topic_state.get(
            "topic_status",
            "",
        )

        # 仅首条消息做意图识别，追问消息跳过（直接走 LangGraph）
        if is_first_topic_turn:
            yield _sse_req({"type": "status", "text": "正在识别意图..."})
            try:
                intent_result = classify_intent(message)
            except Exception as error:
                log_node_degraded(
                    "intent_classifier",
                    error,
                    error_code="INTENT_CLASSIFIER_DEGRADED",
                    fallback="query",
                )
                intent_result = type("F", (), {"intent": "query", "quick_reply": ""})()

            if intent_result.intent == "chat":
                reply = intent_result.quick_reply or "你好！有什么可以帮你的吗？"
                session["messages"].append({"role": "user", "content": message, "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S")})
                session["messages"].append({"role": "assistant", "content": reply, "sql": "", "thinking": "[intent] chat", "evaluator": None, "request_id": request_id, "thinking_seconds": round(elapsed_ms(request_timer) / 1000), "status": "chat", "error_message": "", "can_chart": False, "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S")})
                _persist_conversation(conversation_id)
                log_request_end(
                    result_type="chat",
                    summary={"nodes": 0, "intent": "chat"},
                    ms=elapsed_ms(request_timer),
                )
                # chat 快速回复为短文本，直接 done 一次性下发（不走 planner 定稿，无真流式）
                yield _sse_req({"type": "done", "content": reply, "sql": "", "thinking": "[intent] chat", "evaluator": None, "dialogue_id": 0, "llm_tokens": {}})
                return

        state_input = {
            # 身份字段
            "conversation_id": conversation_id,
            "topic_id": topic_id,
            "request_id": request_id,

            # Topic问题字段
            "current_user_input": message,

            # Topic业务记忆由Checkpoint自动恢复
        }

        # 本轮先占位落盘：用户提问 + "处理中"占位回答立即写入 MySQL，
        # 刷新/切换会话不丢失；查询完成后再更新同一轮（round_no 不变），保持"一问一答=一轮"
        session["messages"].append({"role": "user", "content": message, "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S")})
        session["messages"].append({
            "role": "assistant", "content": "", "sql": "", "thinking": "",
            "dialogue_id": 0, "evaluator": None, "llm_tokens": {},
            "request_id": request_id, "thinking_seconds": 0,
            "status": "processing", "error_message": "", "can_chart": False,
            "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _persist_conversation(conversation_id)

        # 注册为进行中请求：前端刷新/切回后通过 /api/chat/status 轮询恢复实时状态
        ACTIVE_REQUESTS[conversation_id] = {
            "request_id": request_id,
            "status": "AI 正在思考...",
            "thinking_parts": [],
            "thinking": "",
            "content": "",
            "sql": "",
            "llm_tokens": {},
            "context": None,
            "cancel_requested": False,
        }
        broadcast_conversations()
        # 查询链路移到后台线程执行：LLM token 在节点内部实时推送到总线，
        # SSE 线程只负责转发，前端才能逐字展示思考过程与最终回答
        bus = StreamBus(sink=make_active_sink(ACTIVE_REQUESTS, conversation_id, request_id))
        # 注册当前请求的流式总线：用户点停止时可立即关闭 SSE 通道，让前后台快速收尾
        ACTIVE_BUSES[conversation_id] = bus
        worker = threading.Thread(
            target=_run_query_worker,
            args=(bus, state_input, observed_topic_status, request_timer, request_started_at),
            daemon=True,
        )
        worker.start()
        try:
            for event in bus.iter_events():
                yield _sse_req(event)
        finally:
            # 客户端断开或异常时通知后台线程停止推送，避免事件堆积
            bus.close()
            # 仅当注册表仍指到本请求的总线时才移除，避免误删并发新请求的总线
            if ACTIVE_BUSES.get(conversation_id) is bus:
                ACTIVE_BUSES.pop(conversation_id, None)

    def _run_query_worker(bus, state_input, observed_topic_status, request_timer, request_started_at):
        """后台执行查询链路：节点事件与 LLM token 写入总线，由 SSE 线程转发。"""
        # 后台线程重新绑定日志上下文与流式总线，节点日志与 token 回调才能正确工作
        worker_token = bind_log_context(
            conversation_id=conversation_id,
            topic_id=topic_id,
            request_id=request_id,
            graph_thread_id=graph_thread_id,
        )
        bind_stream_bus(bus)
        thinking_parts = ["[intent] query"]
        seen = set()          # 出现过哪些节点（用于 evaluator/sql 判断）
        node_seq = {}         # 节点出现次数：0 行自愈等回环时展示为 node#2 区分轮次
        advisor_history_recorded = False
        cancelled = False
        def _full_thinking():
            # 最终落盘时合并流式推理内容：thinking_parts 只含节点摘要，
            # 流式期间推送的 reasoning token 已由镜像累积在 ACTIVE_REQUESTS.thinking
            _base = "\n".join(thinking_parts)
            _stream = str((ACTIVE_REQUESTS.get(conversation_id) or {}).get("thinking") or "")
            _stream = _stream.strip()
            if _stream and _stream not in _base:
                return _base + "\n" + _stream
            return _base
        try:
            for chunk in APP.stream(state_input, config, subgraphs=True):
                # 用户点击停止生成：每个 chunk 间检查取消标志，命中则不再消费后续节点
                if is_cancel_requested(ACTIVE_REQUESTS, conversation_id, request_id):
                    cancelled = True
                    break
                node_dict = chunk[1] if isinstance(chunk, tuple) else chunk
                for node_name, node_update in node_dict.items():
                    # 从LangGraph节点增量更新中统一观察Topic状态变化
                    if isinstance(node_update, dict):
                        next_topic_status = node_update.get(
                            "topic_status",
                            "",
                        )

                        if (
                                next_topic_status
                                and next_topic_status != observed_topic_status
                        ):
                            log_state_change(
                                node_name=node_name,
                                field_name="topic_status",
                                previous_value=observed_topic_status,
                                current_value=next_topic_status,
                            )
                            observed_topic_status = next_topic_status

                    if not node_name:
                        continue
                    # 折叠 execute_query 工具内部的 Seeker 执行链节点：不作为主流程步骤展示，
                    # 也不观察其内部 topic_status（避免子图内部状态污染前端阶段显示）
                    if node_name in _SEEKER_INTERNAL_NODES:
                        seen.add(node_name)
                        continue
                    seen.add(node_name)
                    # 回环轮次计数：首轮用节点名，0 行自愈等二次出现用 node#2 区分
                    seq = node_seq.get(node_name, 0) + 1
                    node_seq[node_name] = seq
                    display_node = node_name if seq == 1 else f"{node_name}#{seq}"
                    label = NODE_LABELS.get(node_name, node_name)
                    if node_name in ("advisor", "advisor_agent"):
                        # Advisor 思考内容已由 token 流逐字展示（标签在首个 token 前发出），
                        # 开启流式时实时节点事件跳过避免重复；关闭流式时整段发送保证可读
                        if not advisor_history_recorded:
                            advisor_history_recorded = True
                            detail = _extract_node_detail(node_name, node_update)
                            thinking_parts.append(
                                "[" + display_node + "] " + label
                                + ("\n" + detail if detail else "")
                            )
                            # 非流式时整段发送，避免关闭流式后 advisor 无内容可见
                            if not get_stream_output_enabled():
                                display_text = label + ("\n" + detail if detail else "")
                                bus.emit({"type": "thinking", "node": display_node, "text": display_text})
                        else:
                            thinking_parts.append("[" + display_node + "] " + label)
                    else:
                        detail = _extract_node_detail(node_name, node_update)
                        display_text = label + ("\n" + detail if detail else "")
                        thinking_parts.append("[" + display_node + "] " + display_text)
                        bus.emit({"type": "thinking", "node": display_node, "text": display_text})

            final_state = APP.get_state(config)
            result = (final_state and final_state.values) or {}

            route = result.get("route", "execute")
            topic_status = result.get("topic_status", "")
            final_answer = result.get("final_answer", "")
            generated_sql = result.get("generated_sql", "")
            # 停止可能在最后一个 chunk 消费完后、落盘前才到达：落盘前再校验一次取消标志
            if is_cancel_requested(ACTIVE_REQUESTS, conversation_id, request_id):
                cancelled = True
            if cancelled:
                # 已停止生成：使用已流出的部分回答作为最终内容，标记 aborted 状态
                final_answer = _cancel_partial_content(conversation_id)
                generated_sql = ""
            final_status = "aborted" if cancelled else "success"
            # 优先展示本轮实际执行过的 SQL（execute_query 工具收集 → Planner 写入 executed_sql）：
            # 单条直接展示，多条（多段/并行）按代码块分开展示，每条带步骤/表名/行数标题。
            # executed_sql 由 Planner 每轮重新写入，本轮未查数为空列表，天然不残留上一轮 SQL。
            executed_sql = result.get("executed_sql") or []
            if executed_sql:
                if len(executed_sql) == 1:
                    display_sql = str(executed_sql[0].get("sql") or "")
                else:
                    _sql_blocks = []
                    for _i, _e in enumerate(executed_sql, 1):
                        _title = f"步骤 {_i}"
                        if _e.get("step_id"):
                            _title += f" · {_e.get('step_id')}"
                        if _e.get("table"):
                            _title += f" · {_e.get('table')}"
                        _title += f"（{_e.get('row_count', 0)} 行）"
                        _sql_blocks.append(f"{_title}\n```sql\n{_e.get('sql', '')}\n```")
                    display_sql = "\n\n".join(_sql_blocks)
            else:
                # 兜底：旧链路（generated_sql + 节点判定）无 executed_sql 时沿用原逻辑
                sql_query_nodes = {
                    "retrieve_schema",
                    "generate_sql",
                    "validate_sql",
                    "prepare_sql_fix",
                    "execute_sql",
                    "prepare_sql_exec_fix",
                    "persist_result",
                }
                has_sql_query = bool(seen & sql_query_nodes)
                display_sql = generated_sql if has_sql_query else ""
            # Evaluator 已并入 Planner（单 Agent），评分功能移除：统一空值兼容前端
            evaluator_payload = None
            dialogue_id = 0

            # 请求级 LLM token 汇总（在 log_request_end 清理聚合器之前读取）
            llm_tokens = get_llm_token_usage()
            # 落盘前再校验一次取消标志：防止最后时刻用户点停止但 success 分支已进入而覆盖 aborted
            if is_cancel_requested(ACTIVE_REQUESTS, conversation_id, request_id):
                cancelled = True
                final_status = "aborted"
                final_answer = _cancel_partial_content(conversation_id)
                display_sql = ""
            # 覆盖本轮开头的"处理中"占位回答（round_no 不变），不新增轮次
            # 成功轮回填 can_chart：仅当该请求存在可自动成图的落盘结果时为 True，
            # 前端据此决定是否显示『生成图表』按钮（无法生成就不展示）
            if _is_current_request(conversation_id, request_id):
                from agentTest.langgraph_app.tools.chart_tool import has_chartable_result
                session["messages"][-1] = {
                    "role": "assistant", "content": final_answer, "sql": display_sql,
                    "thinking": _full_thinking(),
                    "dialogue_id": dialogue_id,
                    "evaluator": evaluator_payload,
                    "llm_tokens": llm_tokens,
                    "request_id": request_id,
                    "thinking_seconds": round(elapsed_ms(request_timer) / 1000),
                    "status": final_status,
                    "error_message": "",
                    "can_chart": False if cancelled else has_chartable_result(conversation_id, request_id),
                    "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _persist_conversation(conversation_id)

            # ── 去 Topic 化：不再按 new_query / 异常终态切换 Topic，
            #    整个对话共享历史与状态，新问数由 Planner 每轮重新改写 effective_query ──

            log_request_end(
                result_type="query",
                route=route,
                topic_status=topic_status,
                summary={"nodes": len(seen), "route": route, "topic_status": topic_status},
                ms=elapsed_ms(request_timer),
            )

            # 最终回答的真流式已由 LLM 层（ThinkingStreamChatModel.answer_field）在生成时
            # 实时 emit（live=true），done 事件整体下发作为兜底，无需再重放
            bus.emit({
                "type": "done",
                "content": final_answer,
                "sql": display_sql,
                "topic_status": topic_status,
                "thinking": "\n".join(thinking_parts),
                "evaluator": evaluator_payload,
                "dialogue_id": dialogue_id,
                "llm_tokens": llm_tokens,
                "status": final_status,
                "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as error:
            # 用户点停止导致的 SQL 中断：一律按 aborted 处理，不暴露错误编号
            if is_cancel_requested(ACTIVE_REQUESTS, conversation_id, request_id):
                _partial = _cancel_partial_content(conversation_id)
                if _is_current_request(conversation_id, request_id):
                    session["messages"][-1] = {
                        "role": "assistant", "content": _partial,
                        "thinking": _full_thinking(), "sql": "", "dialogue_id": 0,
                        "evaluator": None, "llm_tokens": {}, "request_id": request_id,
                        "thinking_seconds": round(elapsed_ms(request_timer) / 1000),
                        "status": "aborted", "error_message": "", "can_chart": False,
                        "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    _persist_conversation(conversation_id)
                log_request_end(
                    result_type="aborted",
                    route="execute",
                    topic_status=observed_topic_status,
                    summary={"aborted": True},
                    ms=elapsed_ms(request_timer),
                )
                bus.emit({
                    "type": "done", "content": _partial, "sql": "",
                    "thinking": "\n".join(thinking_parts), "evaluator": None,
                    "dialogue_id": 0, "llm_tokens": {}, "status": "aborted",
                    "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
                })
                return
            error_id = uuid.uuid4().hex
            previous_topic_status = observed_topic_status
            try:
                # 异常可能发生在部分节点已经完成后，重新读取最新状态
                latest_snapshot = APP.get_state(config)
                latest_state = (latest_snapshot and latest_snapshot.values) or {}
                previous_topic_status = latest_state.get(
                    "topic_status",
                    previous_topic_status,
                )
                # State只保存安全错误编号，不保存内部异常文本
                APP.update_state(
                    config,
                    {
                        "topic_status": "failed",
                        "error_message": f"{QUERY_ERROR_CODE}:{error_id}",
                    },
                )
                if previous_topic_status != "failed":
                    log_state_change(
                        node_name="request_boundary",
                        field_name="topic_status",
                        previous_value=previous_topic_status,
                        current_value="failed",
                    )
            except Exception as state_error:
                # Checkpoint写入失败不能覆盖最初的业务异常
                log_node_degraded(
                    "request_boundary",
                    state_error,
                    error_code="FAILED_STATE_PERSIST_DEGRADED",
                    related_error_id=error_id,
                    stage="persist_failed_state",
                )
            # 去 Topic 化：失败后不切换 Topic，下一条消息在同一对话内重试
            log_request_error(
                error=error,
                error_id=error_id,
                error_code=QUERY_ERROR_CODE,
                topic_status="failed",
                ms=elapsed_ms(request_timer),
            )
            # 失败轮也落盘审计记录：报错信息/执行时间/用户名称随明细存储
            # 覆盖本轮开头的"处理中"占位回答（round_no 不变），不新增轮次
            if _is_current_request(conversation_id, request_id):
                session["messages"][-1] = {
                    "role": "assistant",
                    "content": "",
                    "thinking": _full_thinking(),
                    "sql": "",
                    "dialogue_id": 0,
                    "evaluator": None,
                    "llm_tokens": {},
                    "request_id": request_id,
                    "thinking_seconds": round(elapsed_ms(request_timer) / 1000),
                    "status": "failed",
                    "error_message": f"{QUERY_ERROR_CODE}:{error_id}",
                    "can_chart": False,
                    "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _persist_conversation(conversation_id)
            bus.emit({
                "type": "error",
                "text": QUERY_SAFE_ERROR_MESSAGE,
                "error_code": QUERY_ERROR_CODE,
                "error_id": error_id,
                "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
            })
        finally:
            reset_log_context(worker_token)
            bus.close()
            clear_cancel_flag(conversation_id, request_id)
            # 查询结束：从进行中注册表移除，前端轮询据此判定完成并重新加载。
            # 仅当记录仍归属本请求时才移除，避免旧 worker 误删用户停止后立即发起的新请求
            if (ACTIVE_REQUESTS.get(conversation_id) or {}).get("request_id") == request_id:
                ACTIVE_REQUESTS.pop(conversation_id, None)
            broadcast_conversations()


    def generate_with_log_context():
        # 为本次流式请求绑定独立日志上下文，避免并发日志相互混淆
        context_token = bind_log_context(
            conversation_id=conversation_id,
            topic_id=topic_id,
            request_id=request_id,
            graph_thread_id=graph_thread_id,
        )
        request_timer = start_timer()
        # 记录本轮请求开始时间，随明细落盘用于审计“执行时间”
        request_started_at = datetime.now()
        topic_state = {}

        try:
            log_request_start(
                input_length=len(message),
                input=message,
            )

            # Checkpoint读取也属于统一请求异常边界
            checkpoint_snapshot = APP.get_state(config)
            topic_state = (
                checkpoint_snapshot
                and checkpoint_snapshot.values
            ) or {}

            yield from generate(
                request_timer,
                topic_state,
                request_started_at,
            )

        except Exception as error:
            error_id = uuid.uuid4().hex
            previous_topic_status = topic_state.get(
                "topic_status",
                "",
            )

            try:
                # 异常可能发生在部分节点已经完成后，重新读取最新状态
                latest_snapshot = APP.get_state(config)
                latest_state = (
                    latest_snapshot
                    and latest_snapshot.values
                ) or {}
                previous_topic_status = latest_state.get(
                    "topic_status",
                    previous_topic_status,
                )

                # State只保存安全错误编号，不保存内部异常文本
                APP.update_state(
                    config,
                    {
                        "topic_status": "failed",
                        "error_message": (
                            f"{QUERY_ERROR_CODE}:{error_id}"
                        ),
                    },
                )

                if previous_topic_status != "failed":
                    log_state_change(
                        node_name="request_boundary",
                        field_name="topic_status",
                        previous_value=previous_topic_status,
                        current_value="failed",
                    )

            except Exception as state_error:
                # Checkpoint写入失败不能覆盖最初的业务异常
                log_node_degraded(
                    "request_boundary",
                    state_error,
                    error_code="FAILED_STATE_PERSIST_DEGRADED",
                    related_error_id=error_id,
                    stage="persist_failed_state",
                )

            # 去 Topic 化：失败后不切换 Topic，下一条消息在同一对话内重试

            log_request_error(
                error=error,
                error_id=error_id,
                error_code=QUERY_ERROR_CODE,
                topic_status="failed",
                ms=elapsed_ms(request_timer),
            )

            # 外层异常兜底：若本轮已写入"处理中"占位回答（worker 未启动等场景），
            # 更新为 failed，避免前端长期显示处理中
            _last = session["messages"][-1] if session["messages"] else {}
            if (isinstance(_last, dict)
                    and _last.get("role") == "assistant"
                    and _last.get("status") == "processing"
                    and _last.get("request_id") == request_id):
                _last.update({
                    "status": "failed",
                    "error_message": f"{QUERY_ERROR_CODE}:{error_id}",
                    "thinking_seconds": round(elapsed_ms(request_timer) / 1000),
                })
                _persist_conversation(conversation_id)

            # 进行中注册表同步清理（worker 未启动/被中断时避免残留）。
            # 仅当记录仍归属本请求时才移除，避免旧 SSE 线程误删停止后立即发起的新请求
            if (ACTIVE_REQUESTS.get(conversation_id) or {}).get("request_id") == request_id:
                ACTIVE_REQUESTS.pop(conversation_id, None)
            broadcast_conversations()

            yield _sse_req({
                "type": "error",
                "text": QUERY_SAFE_ERROR_MESSAGE,
                "error_code": QUERY_ERROR_CODE,
                "error_id": error_id,
            })

        finally:
            # 流式响应正常结束、异常或客户端断开时都释放日志上下文
            reset_log_context(context_token)

    return Response(
        stream_with_context(generate_with_log_context()),
        mimetype="text/event-stream",
    )

@app.route("/api/score", methods=["POST"])
def submit_score():
    data = request.get_json()
    conversation_id = data.get("conversation_id", "")
    score = data.get("score", 0)
    dialogue_id = data.get("dialogue_id", 0)
    if conversation_id not in sessions: return jsonify({"error": "invalid conversation_id"}), 400
    if not isinstance(score, (int, float)) or score < 1 or score > 5: return jsonify({"error": "score 1-5"}), 400
    for msg in reversed(sessions[conversation_id]["messages"]):
        if msg["role"] == "assistant":
            if msg.get("evaluator") is None: msg["evaluator"] = {}
            msg["evaluator"]["user_score"] = score
            break
    _persist_conversation(conversation_id)
    if dialogue_id:
        try:
            result = update_user_score(dialogue_id, score * 20)
            # FAISS 同步：原先高分变低分则删，原先低分变高分则加
            if result and result.get("was_high") != result.get("is_high"):
                example_store = RUNTIME.get("example_vector_store")
                if example_store:
                    hash_id = result.get("hash_id", "")
                    if hash_id:
                        example_store.sync_by_score(
                            hash_id=hash_id,
                            question=result.get("question", ""),
                            sql=result.get("sql", ""),
                            answer=result.get("answer", ""),
                            tables=result.get("tables", []),
                            fields=result.get("fields", []),
                            domain_tag=result.get("domain_tag", ""),
                            effective_query=result.get("effective_query", ""),
                            score=result.get("score", 0),
                            is_high=result.get("is_high", False),
                        )
                        print(f"[server] FAISS synced: was_high={result.get('was_high')} -> is_high={result.get('is_high')}")
        except Exception as e:
            print(f"[server] update score failed: {e}")
    return jsonify({"success": True})

def _lan_access_urls():
    """查找本机内网可访问的 IPv4 地址，供启动时提示同事访问。"""
    candidates = []
    def _add(ip):
        ip = str(ip or "")
        if ip and ip not in candidates:
            candidates.append(ip)
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            _add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        _add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    results = []
    for ip in candidates:
        if ip.startswith("127.") or ip.startswith("169.254.") or ip.startswith("198.18."):
            continue
        if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", ip):
            results.append(ip)
    # 无私网地址时回退展示所有非回环地址，便于同事尝试
    if not results:
        results = [ip for ip in candidates if not ip.startswith("127.")]
    return results


if __name__ == "__main__":
    print("[server] starting at http://localhost:5000")
    lan_ips = _lan_access_urls()
    if lan_ips:
        print("[server] 同一局域网打开以下地址：")
        for _ip in lan_ips:
            print(f"[server]    http://{_ip}:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
