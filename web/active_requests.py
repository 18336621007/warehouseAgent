# 进行中请求注册表：conversation_id -> {request_id, status, thinking, content, ...}
# 供前端刷新/切换会话后轮询恢复进行中状态（仿 codex 的进行中任务可见）
# 同时供网页端 /api/chat/status 与飞书机器人共享，飞书会话的实时思考也能在后端注册表更新
ACTIVE_REQUESTS = {}


def make_active_sink(active_requests, conversation_id, request_id=""):
    """构造 StreamBus 镜像回调：把本请求的事件增量累积进指定注册表快照。

    客户端断开（刷新/关闭）后 SSE 线程关闭了总线，但后台 worker 仍继续产出事件；
    镜像在总线关闭判断之前记录，保证轮询端始终能拿到最新思考/回答进度。
    网页端与飞书查询复用同一实现，只是注册表由调用方传入（飞书传共享 ACTIVE_REQUESTS）。
    """
    def sink(event):
        snap = active_requests.setdefault(conversation_id, {
            "request_id": "",
            "status": "AI 正在思考...",
            "thinking_parts": [],
            "thinking": "",
            "content": "",
            "sql": "",
            "llm_tokens": {},
            "context": None,
            "cancel_requested": False,
        })
        # cancel_chat 清除注册表后若仍收到尾部事件，镜像会重建快照：
        # 补回请求号与已停止标记，保证 worker 取消判定与 finally 清理始终生效
        if request_id and not snap.get("request_id"):
            snap["request_id"] = request_id
        if request_id:
            _flag = CANCEL_FLAGS.get(conversation_id)
            if _flag and _flag.get("request_id") == str(request_id):
                snap["cancel_requested"] = True
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

# 节点展示名称：LangGraph 各节点 → 前端「思考过程」步骤文案
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


# 停止请求的独立标记：cancel_chat 立即落盘 aborted 并清除 ACTIVE_REQUESTS 后，
# 后台 worker 仍能据此识别"用户已停止本请求"，从而按 aborted 收尾，不再被误写成 failed/success。
CANCEL_FLAGS = {}


def set_cancel_flag(conversation_id: str, request_id: str, content: str = "") -> None:
    """登记已请求停止的会话+请求号（跨线程可读，由 worker finally 清理）。

    content 保存停止时已流出的部分回答：cancel_chat 会立即清除 ACTIVE_REQUESTS，
    worker 收尾时需从此处兜底读取，避免把已保留的部分回答覆盖成"已停止生成"。
    """
    if conversation_id and request_id:
        CANCEL_FLAGS[conversation_id] = {
            "request_id": request_id,
            "content": content,
        }


def clear_cancel_flag(conversation_id: str, request_id: str) -> None:
    """清理停止标记，仅当仍属于同一请求号时删除，避免误清新请求标志。"""
    flag = CANCEL_FLAGS.get(conversation_id)
    if flag and flag.get("request_id") == request_id:
        CANCEL_FLAGS.pop(conversation_id, None)


def is_cancel_requested(active_requests, conversation_id, request_id="") -> bool:
    """判断某会话是否有已请求的取消标志（供后台 graph 线程在流式迭代中检查）。

    优先看 ACTIVE_REQUESTS 内的实时标志；cancel_chat 清除注册表后，
    再按 request_id 匹配独立 CANCEL_FLAGS，保证 worker 仍能识别本请求已停止。
    """
    snap = active_requests.get(conversation_id)
    if snap and snap.get("cancel_requested"):
        return True
    if request_id:
        flag = CANCEL_FLAGS.get(conversation_id)
        if flag and flag.get("request_id") == str(request_id):
            return True
    return False
