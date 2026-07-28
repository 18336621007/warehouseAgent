"""
ChatGPT UI backend - Flask API (streaming + scoring + rename/delete)
"""
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
import uuid, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentTest.langgraph_app.graphs.supervisor_graph import build_supervisor_graph
from agentTest.langgraph_app.runtime.graph_runtime import build_graph_runtime
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
    "planner": "正在分析查询需求...",
    "advisor": "需求不够明确，正在检索相关信息...",
    "retrieve_schema": "正在检索数据表结构...",
    "enrich_schema_context": "正在补充字段信息...",
    "generate_sql": "正在生成 SQL...",
    "validate_sql": "正在校验 SQL...",
    "prepare_sql_fix": "SQL 需修正，正在重新生成...",
    "execute_sql": "正在 Hive 中执行查询...",
    "build_final_answer": "正在整理查询结果...",
    "evaluator": "正在评估对话质量...",
}


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
    tid = str(uuid.uuid4())[:8]
    sessions[tid] = {"original_question": "", "advisor_turns": 0, "advisor_last_answer": "", "messages": []}
    return jsonify({"thread_id": tid})


@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    convs = []
    for tid, sess in sessions.items():
        first = sess.get("title_override") or (sess["messages"][0]["content"] if sess["messages"] else "New Chat")
        convs.append({"thread_id": tid, "title": first[:50], "message_count": len(sess["messages"])})
    return jsonify({"conversations": convs})


@app.route("/api/conversations/<tid>", methods=["PUT"])
def rename_conversation(tid):
    data = request.get_json()
    title = (data.get("title") or "").strip()
    if tid not in sessions: return jsonify({"error": "invalid"}), 400
    if title: sessions[tid]["title_override"] = title
    return jsonify({"success": True})


@app.route("/api/conversations/<tid>", methods=["DELETE"])
def delete_conversation(tid):
    if tid in sessions: del sessions[tid]
    return jsonify({"success": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    thread_id = data.get("thread_id", "")
    message = data.get("message", "").strip()
    if not thread_id or thread_id not in sessions: return jsonify({"error": "invalid thread_id"}), 400
    if not message: return jsonify({"error": "empty message"}), 400

    session = sessions[thread_id]

    def generate():
        # 仅首条消息做意图识别，追问消息跳过（直接走 LangGraph）
        if not session["original_question"]:
            yield _sse({"type": "status", "text": "正在识别意图..."})
            try:
                intent_result = classify_intent(message)
            except Exception as e:
                print(f"[server] intent failed: {e}")
                intent_result = type("F", (), {"intent": "query", "quick_reply": ""})()

            if intent_result.intent == "chat":
                reply = intent_result.quick_reply or "你好！有什么可以帮你的吗？"
                session["messages"].append({"role": "user", "content": message})
                session["messages"].append({"role": "assistant", "content": reply, "sql": "", "thinking": "[intent] chat", "evaluator": None})
                yield _sse({"type": "done", "content": reply, "sql": "", "thinking": "[intent] chat", "evaluator": None, "dialogue_id": 0})
                return

        # ── query: LangGraph pipeline ──
        if not session["original_question"]:
            session["original_question"] = message

        config = {"configurable": {"thread_id": thread_id}}
        state_input = {
            "question": session["original_question"],
            "original_question": session["original_question"],
            "user_response": message,
            "advisor_last_answer": session["advisor_last_answer"],
            "advisor_turns": session["advisor_turns"],
        }

        thinking_parts = ["[intent] query"]
        seen = set()

        try:
            for chunk in APP.stream(state_input, config, subgraphs=True):
                node_dict = chunk[1] if isinstance(chunk, tuple) else chunk
                for node_name, _ in node_dict.items():
                    if not node_name or node_name in seen: continue
                    seen.add(node_name)
                    label = NODE_LABELS.get(node_name, node_name)
                    thinking_parts.append("[" + node_name + "] " + label)
                    yield _sse({"type": "thinking", "node": node_name, "text": label})

            final_state = APP.get_state(config)
            result = (final_state and final_state.values) or {}
        except Exception as e:
            yield _sse({"type": "error", "text": str(e)})
            return

        route = result.get("route", "seeker")
        final_answer = result.get("final_answer", "")
        generated_sql = result.get("generated_sql", "")
        ev_score = result.get("evaluator_score", 0)
        ev_self = result.get("evaluator_self_score", 0)
        dialogue_id = result.get("evaluator_dialogue_id", 0)

        session["messages"].append({"role": "user", "content": message})
        session["messages"].append({
            "role": "assistant", "content": final_answer, "sql": generated_sql,
            "thinking": "\n".join(thinking_parts), "dialogue_id": dialogue_id,
            "evaluator": {"score": ev_score, "self_score": ev_self} if ev_score else None,
        })

        session["advisor_last_answer"] = final_answer
        session["advisor_turns"] = session["advisor_turns"] + 1 if route == "advisor" else 0

        yield _sse({
            "type": "done", "content": final_answer, "sql": generated_sql,
            "thinking": "\n".join(thinking_parts),
            "evaluator": {"score": ev_score, "self_score": ev_self} if ev_score else None,
            "dialogue_id": dialogue_id,
        })

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/score", methods=["POST"])
def submit_score():
    data = request.get_json()
    thread_id = data.get("thread_id", "")
    score = data.get("score", 0)
    dialogue_id = data.get("dialogue_id", 0)
    if thread_id not in sessions: return jsonify({"error": "invalid thread_id"}), 400
    if not isinstance(score, (int, float)) or score < 1 or score > 5: return jsonify({"error": "score 1-5"}), 400
    for msg in reversed(sessions[thread_id]["messages"]):
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