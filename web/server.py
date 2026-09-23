"""
ChatGPT UI backend - Flask API (streaming + scoring + rename/delete)
"""
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
import uuid, os, sys, json, threading
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


# 进行中请求注册表：conversation_id -> {request_id, status, thinking, content, ...}
# 供前端刷新/切换会话后轮询恢复进行中状态（仿 codex 的进行中任务可见）
ACTIVE_REQUESTS = {}


def _make_active_sink(conversation_id):
    """构造 StreamBus 镜像回调：把本请求的事件增量累积进 ACTIVE_REQUESTS 快照。

    客户端断开（刷新/关闭）后 SSE 线程关闭了总线，但后台 worker 仍继续产出事件；
    镜像在总线关闭判断之前记录，保证轮询端始终能拿到最新思考/回答进度。
    """
    def sink(event):
        snap = ACTIVE_REQUESTS.setdefault(conversation_id, {
            "request_id": "",
            "status": "AI 正在思考...",
            "thinking_parts": [],
            "thinking": "",
            "content": "",
            "sql": "",
            "llm_tokens": {},
            "context": None,
        })
        etype = event.get("type")
        parts = snap["thinking_parts"]
        if etype in ("status", "thinking"):
            text = event.get("text") or ""
            if text:
                first = text.split("\n")[0]
                if first:
                    snap["status"] = first
                parts.append({"sid": None, "text": text})
                snap["thinking"] = "\n".join(p["text"] for p in parts)
        elif etype == "token":
            if event.get("scope") == "answer":
                snap["content"] += event.get("text") or ""
            else:
                sid = event.get("stream_id") or ""
                text = event.get("text") or ""
                if text:
                    if sid:
                        hit = None
                        for p in reversed(parts):
                            if p["sid"] == sid:
                                hit = p
                                break
                        if hit:
                            hit["text"] += text
                        else:
                            parts.append({"sid": sid, "text": text})
                    else:
                        parts.append({"sid": None, "text": text})
                    snap["thinking"] = "\n".join(p["text"] for p in parts)
        elif etype == "thinking_retract":
            rsid = event.get("stream_id") or ""
            snap["thinking_parts"] = [p for p in parts if p["sid"] != rsid]
            snap["thinking"] = "\n".join(p["text"] for p in snap["thinking_parts"])
        elif etype == "done":
            snap["content"] = event.get("content") or snap["content"]
            snap["status"] = "done"
            snap["sql"] = event.get("sql") or ""
            snap["llm_tokens"] = event.get("llm_tokens") or {}
        elif etype == "error":
            snap["status"] = "error"
            snap["content"] = (event.get("text") or "") + (
                ("\n错误编号：" + event.get("error_id", "")) if event.get("error_id") else ""
            )
        elif etype == "context_progress":
            snap["context"] = {
                "used_tokens": event.get("used_tokens"),
                "window_tokens": event.get("window_tokens"),
                "percent": event.get("percent"),
            }
    return sink


def _persist_conversation(conversation_id):
    """落盘会话记录；MySQL 异常仅记日志，不阻塞聊天主流程。"""
    try:
        upsert_conv(conversation_id, sessions[conversation_id])
    except Exception as error:
        print(f"[server] persist conversation failed: {error}")


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

NODE_LABELS = {
    "capture_user_message": "正在记录本轮问题...",
    "planner": "正在分析查询需求...",
    "retrieve_schema": "正在检索数据表结构...",
    "enrich_schema_context": "正在补充字段信息...",
    "generate_sql": "正在生成 SQL...",
    "validate_sql": "正在校验 SQL...",
    "prepare_sql_fix": "SQL 需修正，正在重新生成...",
    "execute_sql": "正在执行查询...",
    "persist_result": "正在落盘查询结果...",
    "query_error_fallback": "查询未能完成，正在整理错误信息...",
    "evaluator": "正在评估对话质量...",
}

# execute_query 工具内部 Seeker 执行链节点：折叠为工具的单一事件，不作为主流程步骤展示
_SEEKER_INTERNAL_NODES = {
    "retrieve_schema",
    "generate_sql",
    "validate_sql",
    "prepare_sql_fix",
    "execute_sql",
    "prepare_sql_exec_fix",
    "persist_result",
    "query_error_end",
    "end_plan_error",
}

# 前端只展示安全错误信息，内部异常通过error_id在日志中定位
QUERY_ERROR_CODE = "QUERY_EXECUTION_FAILED"
QUERY_SAFE_ERROR_MESSAGE = "系统暂时无法完成本次查询，请稍后重试。"

def _sse(data_dict):
    return "data: " + json.dumps(data_dict, ensure_ascii=False) + "\n\n"

def _extract_node_detail(node_name, node_update):
    """从节点写入 State 的增量中提取可展示的 LLM 输出，作为前端思考过程内容。

    只取确定性的结构化输出字段，避免把整个 State 塞给前端。
    """
    if not isinstance(node_update, dict):
        return ""
    parts = []
    if node_name == "planner":
        if node_update.get("planner_reason"):
            parts.append("决策理由: " + str(node_update.get("planner_reason")))
        if node_update.get("route"):
            parts.append("路由: " + str(node_update.get("route")))
        if node_update.get("effective_query"):
            parts.append("有效需求: " + str(node_update.get("effective_query")))
    elif node_name == "generate_sql":
        if node_update.get("generated_sql"):
            parts.append("SQL: " + str(node_update.get("generated_sql")))
    elif node_name == "persist_result":
        # 0 行自愈等旁白：向用户展示"发现空结果 → 返回修正"的思考过程
        if node_update.get("self_heal_note"):
            parts.append(str(node_update.get("self_heal_note")))
    return "\n".join(parts)

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
    # 产品接口统一使用conversation_id，避免与LangGraph内部thread_id混淆
    return jsonify({
        "conversation_id": conversation_id,
        "creator": creator,
    })

@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    convs = []
    for conversation_id, sess in sessions.items():
        first = sess.get("title_override") or (sess["messages"][0]["content"] if sess["messages"] else "New Chat")
        convs.append({"conversation_id": conversation_id, "title": first[:50], "message_count": len(sess["messages"]), "creator": sess.get("creator", "")})
    return jsonify({"conversations": convs})

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
    return jsonify({"success": True})

@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id):
    # 软删除：仅前端不可见，MySQL 记录保留并打 deleted_at 标记（供审计）
    if conversation_id in sessions:
        del sessions[conversation_id]
    _soft_delete_conversation(conversation_id)
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
    })

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    conversation_id = data.get("conversation_id", "")
    message = data.get("message", "").strip()
    if not conversation_id or conversation_id not in sessions: return jsonify({"error": "invalid conversation_id"}), 400
    if not message: return jsonify({"error": "empty message"}), 400

    session = sessions[conversation_id]
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
                session["messages"].append({"role": "user", "content": message})
                session["messages"].append({"role": "assistant", "content": reply, "sql": "", "thinking": "[intent] chat", "evaluator": None, "request_id": request_id, "thinking_seconds": round(elapsed_ms(request_timer) / 1000), "status": "chat", "error_message": "", "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S")})
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
        session["messages"].append({"role": "user", "content": message})
        session["messages"].append({
            "role": "assistant", "content": "", "sql": "", "thinking": "",
            "dialogue_id": 0, "evaluator": None, "llm_tokens": {},
            "request_id": request_id, "thinking_seconds": 0,
            "status": "processing", "error_message": "",
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
        }
        # 查询链路移到后台线程执行：LLM token 在节点内部实时推送到总线，
        # SSE 线程只负责转发，前端才能逐字展示思考过程与最终回答
        bus = StreamBus(sink=_make_active_sink(conversation_id))
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
        try:
            for chunk in APP.stream(state_input, config, subgraphs=True):
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
            # 覆盖本轮开头的"处理中"占位回答（round_no 不变），不新增轮次
            session["messages"][-1] = {
                "role": "assistant", "content": final_answer, "sql": display_sql,
                "thinking": "\n".join(thinking_parts),
                "dialogue_id": dialogue_id,
                "evaluator": evaluator_payload,
                "llm_tokens": llm_tokens,
                "request_id": request_id,
                "thinking_seconds": round(elapsed_ms(request_timer) / 1000),
                "status": "success",
                "error_message": "",
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
            })
        except Exception as error:
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
            session["messages"][-1] = {
                "role": "assistant",
                "content": "",
                "thinking": "\n".join(thinking_parts),
                "sql": "",
                "dialogue_id": 0,
                "evaluator": None,
                "llm_tokens": {},
                "request_id": request_id,
                "thinking_seconds": round(elapsed_ms(request_timer) / 1000),
                "status": "failed",
                "error_message": f"{QUERY_ERROR_CODE}:{error_id}",
                "request_at": request_started_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _persist_conversation(conversation_id)
            bus.emit({
                "type": "error",
                "text": QUERY_SAFE_ERROR_MESSAGE,
                "error_code": QUERY_ERROR_CODE,
                "error_id": error_id,
            })
        finally:
            reset_log_context(worker_token)
            bus.close()
            # 查询结束：从进行中注册表移除，前端轮询据此判定完成并重新加载
            ACTIVE_REQUESTS.pop(conversation_id, None)


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

            # 进行中注册表同步清理（worker 未启动/被中断时避免残留）
            ACTIVE_REQUESTS.pop(conversation_id, None)

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

if __name__ == "__main__":
    print("[server] starting at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
