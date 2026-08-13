"""
ChatGPT UI backend - Flask API (streaming + scoring + rename/delete)
"""
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
import uuid, os, sys, json, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentTest.langgraph_app.runtime.graph_logger import bind_log_context
from agentTest.langgraph_app.runtime.graph_logger import reset_log_context
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms
from agentTest.langgraph_app.runtime.graph_logger import log_node_degraded
from agentTest.langgraph_app.runtime.graph_logger import log_request_end
from agentTest.langgraph_app.runtime.graph_logger import log_request_error
from agentTest.langgraph_app.runtime.graph_logger import log_request_start
from agentTest.langgraph_app.runtime.graph_logger import log_state_change
from agentTest.langgraph_app.runtime.graph_logger import start_timer
from agentTest.langgraph_app.graphs.supervisor_graph import build_supervisor_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime
from agentTest.langgraph_app.runtime.stream_bus import StreamBus, bind_stream_bus
from agentTest.config.settings import get_stream_output_enabled
from web.intent_classifier import classify_intent
from agentTest.metadata.mysql_store import update_user_score

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

print("[server] loading LangGraph runtime...")
RUNTIME = build_graph_runtime()
APP = build_supervisor_graph(RUNTIME)
print("[server] runtime ready")

sessions = {}

NODE_LABELS = {
    "capture_user_message": "正在记录本轮问题...",
    "planner": "正在分析查询需求...",
    "advisor": "需求不够明确，正在检索相关信息...",
    "advisor_agent": "正在核验元数据并确认口径...",
    "retrieve_schema": "正在检索数据表结构...",
    "enrich_schema_context": "正在补充字段信息...",
    "generate_sql": "正在生成 SQL...",
    "validate_sql": "正在校验 SQL...",
    "prepare_sql_fix": "SQL 需修正，正在重新生成...",
    "execute_sql": "正在 Hive 中执行查询...",
    "build_final_answer": "正在整理查询结果...",
    "evaluator": "正在评估对话质量...",
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
    elif node_name in ("advisor", "advisor_agent"):
        # ReAct 每步 LLM 输出优先展示，final_answer 是最终回复文本
        for line in (node_update.get("advisor_thinking") or []):
            parts.append(str(line))
        if not parts and node_update.get("final_answer"):
            parts.append(str(node_update.get("final_answer")))
    elif node_name == "generate_sql":
        if node_update.get("generated_sql"):
            parts.append("SQL: " + str(node_update.get("generated_sql")))
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

    # 新对话默认创建第一个问数Topic
    topic_id = uuid.uuid4().hex

    sessions[conversation_id] = {
        "topic_id": topic_id,
        "messages": [],
    }
    # 产品接口统一使用conversation_id，避免与LangGraph内部thread_id混淆
    return jsonify({
        "conversation_id": conversation_id
    })

@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    convs = []
    for conversation_id, sess in sessions.items():
        first = sess.get("title_override") or (sess["messages"][0]["content"] if sess["messages"] else "New Chat")
        convs.append({"conversation_id": conversation_id, "title": first[:50], "message_count": len(sess["messages"])})
    return jsonify({"conversations": convs})

@app.route("/api/conversations/<conversation_id>", methods=["PUT"])
def rename_conversation(conversation_id):
    data = request.get_json()
    title = (data.get("title") or "").strip()
    if conversation_id not in sessions: return jsonify({"error": "invalid"}), 400
    if title: sessions[conversation_id]["title_override"] = title
    return jsonify({"success": True})

@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id):
    if conversation_id in sessions: del sessions[conversation_id]
    return jsonify({"success": True})

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    conversation_id = data.get("conversation_id", "")
    message = data.get("message", "").strip()
    if not conversation_id or conversation_id not in sessions: return jsonify({"error": "invalid conversation_id"}), 400
    if not message: return jsonify({"error": "empty message"}), 400

    session = sessions[conversation_id]
    # 新查数问题创建独立Topic，并使用新的LangGraph Checkpoint
    if session.pop("_new_topic", False):
        session["topic_id"] = uuid.uuid4().hex

    # 当前Topic的多轮追问共享同一个topic_id
    topic_id = session["topic_id"]

    # 每次HTTP请求使用独立request_id
    request_id = uuid.uuid4().hex

    def _sse_req(data_dict):
        # SSE 事件统一携带 request_id，前端可据此关联日志排查问题
        return _sse({**data_dict, "request_id": request_id})

    # 每个Topic拥有独立的LangGraph Checkpoint
    graph_thread_id = f"{conversation_id}:{topic_id}"
    config = {
        "configurable": {
            "thread_id": graph_thread_id,
        }
    }

    def generate(request_timer, topic_state):
        # ── query: LangGraph pipeline ──
        is_first_topic_turn = not topic_state.get("original_question")

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
                session["messages"].append({"role": "assistant", "content": reply, "sql": "", "thinking": "[intent] chat", "evaluator": None})
                log_request_end(
                    result_type="chat",
                    summary={"nodes": 0, "intent": "chat"},
                    ms=elapsed_ms(request_timer),
                )
                yield _sse_req({"type": "done", "content": reply, "sql": "", "thinking": "[intent] chat", "evaluator": None, "dialogue_id": 0})
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

        # 查询链路移到后台线程执行：LLM token 在节点内部实时推送到总线，
        # SSE 线程只负责转发，前端才能逐字展示思考过程与最终回答
        bus = StreamBus()
        worker = threading.Thread(
            target=_run_query_worker,
            args=(bus, state_input, observed_topic_status, request_timer),
            daemon=True,
        )
        worker.start()
        try:
            for event in bus.iter_events():
                yield _sse_req(event)
        finally:
            # 客户端断开或异常时通知后台线程停止推送，避免事件堆积
            bus.close()

    def _run_query_worker(bus, state_input, observed_topic_status, request_timer):
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
        seen = set()
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

                    if not node_name or node_name in seen:
                        continue
                    seen.add(node_name)
                    label = NODE_LABELS.get(node_name, node_name)
                    if node_name in ("advisor", "advisor_agent"):
                        # Advisor 思考内容已由 token 流逐字展示（标签在首个 token 前发出），
                        # 开启流式时实时节点事件跳过避免重复；关闭流式时整段发送保证可读
                        if not advisor_history_recorded:
                            advisor_history_recorded = True
                            detail = _extract_node_detail(node_name, node_update)
                            thinking_parts.append(
                                "[" + node_name + "] " + label
                                + ("\n" + detail if detail else "")
                            )
                            # 非流式时整段发送，避免关闭流式后 advisor 无内容可见
                            if not get_stream_output_enabled():
                                display_text = label + ("\n" + detail if detail else "")
                                bus.emit({"type": "thinking", "node": node_name, "text": display_text})
                        else:
                            thinking_parts.append("[" + node_name + "] " + label)
                    else:
                        detail = _extract_node_detail(node_name, node_update)
                        display_text = label + ("\n" + detail if detail else "")
                        thinking_parts.append("[" + node_name + "] " + display_text)
                        bus.emit({"type": "thinking", "node": node_name, "text": display_text})

            final_state = APP.get_state(config)
            result = (final_state and final_state.values) or {}

            route = result.get("route", "seeker")
            topic_status = result.get("topic_status", "")
            final_answer = result.get("final_answer", "")
            generated_sql = result.get("generated_sql", "")
            ev_score = result.get("evaluator_score", 0)
            ev_self = result.get("evaluator_self_score", 0)
            dialogue_id = result.get("evaluator_dialogue_id", 0)
            # 评分只属于本轮真正执行过 Evaluator 的查询；Evaluator 输出持久化在
            # AgentState 中会跨轮残留，必须按本轮执行节点判断，避免澄清/追问轮重复展示评分
            has_evaluator = "evaluator" in seen
            # generated_sql 同样持久化在 AgentState 中会跨轮残留，只有本轮真正
            # 执行过 Seeker 查询链路时才透传，避免漄清/追问轮展示上一轮的旧 SQL
            sql_query_nodes = {
                "retrieve_schema",
                "generate_sql",
                "validate_sql",
                "prepare_sql_fix",
                "execute_sql",
                "prepare_sql_exec_fix",
                "build_final_answer",
            }
            has_sql_query = bool(seen & sql_query_nodes)
            display_sql = generated_sql if has_sql_query else ""
            evaluator_payload = (
                {"score": ev_score, "self_score": ev_self}
                if (has_evaluator and ev_score)
                else None
            )

            session["messages"].append({"role": "user", "content": message})
            session["messages"].append({
                "role": "assistant", "content": final_answer, "sql": display_sql,
                "thinking": "\n".join(thinking_parts),
                "dialogue_id": dialogue_id if has_evaluator else 0,
                "evaluator": evaluator_payload,
            })

            # ── 根据本轮语义决定下一次查数任务 ──
            # 追问类（plan_refinement / result_follow_up / clarification_explanation）沿用同一 Topic，
            # 保留 confirmed_plan 与历史供下一轮识别；只有真正换话题(new_query)或异常终态才切 Topic。
            follow_up_mode = result.get("follow_up_mode", "")
            if topic_status in ("failed", "cancelled") or (
                    topic_status == "completed" and follow_up_mode == "new_query"
            ):
                # 当前Topic已结束或用户已换话题，下一条消息创建独立Topic
                session["_new_topic"] = True

            log_request_end(
                result_type="query",
                route=route,
                topic_status=topic_status,
                summary={"nodes": len(seen), "route": route, "topic_status": topic_status},
                ms=elapsed_ms(request_timer),
            )

            bus.emit({
                "type": "done",
                "content": final_answer,
                "sql": display_sql,
                "topic_status": topic_status,
                "thinking": "\n".join(thinking_parts),
                "evaluator": evaluator_payload,
                "dialogue_id": dialogue_id if has_evaluator else 0,
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
            # failed是Topic终态，下一条消息创建新的Topic
            session["_new_topic"] = True
            log_request_error(
                error=error,
                error_id=error_id,
                error_code=QUERY_ERROR_CODE,
                topic_status="failed",
                ms=elapsed_ms(request_timer),
            )
            bus.emit({
                "type": "error",
                "text": QUERY_SAFE_ERROR_MESSAGE,
                "error_code": QUERY_ERROR_CODE,
                "error_id": error_id,
            })
        finally:
            reset_log_context(worker_token)
            bus.close()


    def generate_with_log_context():
        # 为本次流式请求绑定独立日志上下文，避免并发日志相互混淆
        context_token = bind_log_context(
            conversation_id=conversation_id,
            topic_id=topic_id,
            request_id=request_id,
            graph_thread_id=graph_thread_id,
        )
        request_timer = start_timer()
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

            # failed是Topic终态，下一条消息创建新的Topic
            session["_new_topic"] = True

            log_request_error(
                error=error,
                error_id=error_id,
                error_code=QUERY_ERROR_CODE,
                topic_status="failed",
                ms=elapsed_ms(request_timer),
            )

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
