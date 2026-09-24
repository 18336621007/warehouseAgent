# -*- coding: utf-8 -*-
# feishu_bot.py —— 飞书长连接机器人接入层
# 复用 web/server.py 的 RUNTIME / APP / sessions：飞书消息落在同一套会话/落盘体系，
# 大标题沿用网页端规则（首条消息），creator 记「飞书-用户名」；回答为文本摘要 + 静态 PNG 图表。
import io
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict

import dotenv
import lark_oapi as lark

from agentTest.langgraph_app.runtime.graph_logger import bind_log_context
from agentTest.langgraph_app.runtime.graph_logger import reset_log_context
from agentTest.langgraph_app.runtime.graph_logger import get_llm_token_usage
from agentTest.langgraph_app.runtime.stream_bus import StreamBus, bind_stream_bus
from web.active_requests import NODE_LABELS, _SEEKER_INTERNAL_NODES, _extract_node_detail, make_active_sink
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

from lark_oapi.api.contact.v3 import GetUserRequest

# 飞书单条文本消息有长度上限，超长只保留结论段（完整数据落盘 CSV 可查）
_TEXT_MAX = 1800


def _load_feishu_config():
    """读取 agentTest/.env 中的飞书凭证，返回 (app_id, app_secret)。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dotenv.load_dotenv(os.path.join(root, "agentTest", ".env"), override=True)  # override：确保以自己的凭证为准
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    return app_id, app_secret


def _collect_messages(sessions: Dict[str, Any], open_id: str, user_name: str = "") -> str:
    """按 open_id 获取稳定 conversation_id；不存在则创建「飞书」会话并落库。

    标题保持与网页端一致（取首条消息），飞书身份信息放在 creator 的「飞书-用户名」上。
    """
    cid = "feishu_" + uuid.uuid5(uuid.NAMESPACE_URL, "feishu://" + open_id).hex
    display = user_name or open_id
    creator = f"飞书-{display}"
    if cid not in sessions:
        sessions[cid] = {
            "topic_id": uuid.uuid4().hex,
            "creator": creator,
            "title_override": "",
            "messages": [],
        }
        _upsert_and_persist(sessions, cid)
    else:
        # 兼容旧版：早期把「飞书-{open_id}」写进了大标题，这里迁移回网页端标题规则
        sess = sessions[cid]
        changed = False
        if str(sess.get("title_override") or "").startswith("飞书-"):
            sess["title_override"] = ""
            changed = True
        if not sess.get("creator") or str(sess.get("creator") or "").startswith("飞书-"):
            sess["creator"] = creator
            changed = True
        if changed:
            _upsert_and_persist(sessions, cid)
    return cid


def _upsert_and_persist(sessions: Dict[str, Any], cid: str):
    """把会话元信息写入 MySQL（会话创建/迁移后调用，异常只打印不影响主流程）。"""
    try:
        from web.conversation_store import upsert
        from web.conversation_events import broadcast_conversations
        upsert(cid, sessions[cid])
        broadcast_conversations()
    except Exception as error:
        print(f"[feishu] persist conversation failed: {error}")


def _parse_text_content(raw: str) -> str:
    """解析飞书 text 消息 content JSON，去掉 @ 标记等富文本噪声后返回纯文本。"""
    try:
        obj = json.loads(raw or "")
    except Exception:
        return ""
    text = str(obj.get("text") or "")
    text = re.sub(r"<at[^>]*>.*?</at>", "", text, flags=re.S)
    text = re.sub(r"@_user_\d+", "", text).strip()
    return text


def _is_group_mention(raw: str) -> bool:
    """群聊中仅 @ 机器人 时才回复（飞书文本里 @ 会渲染成 @_user_N）。"""
    return "@_user_" in (raw or "")


def _persist(sessions: Dict[str, Any], cid: str):
    """写回 MySQL 会话记录，异常只打印不阻断飞书主流程。"""
    try:
        from web.conversation_store import upsert
        from web.conversation_events import broadcast_conversations
        upsert(cid, sessions[cid])
        broadcast_conversations()
    except Exception as error:
        print(f"[feishu] persist conversation failed: {error}")


class FeishuBot:
    """飞书长连接机器人：事件回调 → 复用 LangGraph 查询 → 文字摘要 + PNG 图片。"""

    def __init__(self, runtime, app, sessions, active_requests):
        self.runtime = runtime
        self.app = app
        self.sessions = sessions
        self.active_requests = active_requests
        self._api_client = None
        self._ws_client = None
        self._tenant_token = ""  # 原始 HTTP 发互动卡片用的应用凭证，懒获取后缓存

    def _ensure_clients(self, app_id: str, app_secret: str):
        """创建 HTTP API client（WS client 在独立线程自建循环后创建）。"""
        if self._api_client is None:
            self._api_client = (lark.Client.builder()
                                .app_id(app_id)
                                .app_secret(app_secret)
                                .log_level(lark.LogLevel.WARNING)
                                .build())
        # WS 长连接 client 必须在自建事件循环的线程内创建（内部 asyncio.Lock 绑定线程 loop）

    def start(self) -> None:
        """启动飞书长连接（WS 会在独立线程自建事件循环运行）。"""
        app_id, app_secret = _load_feishu_config()
        if not app_id or not app_secret:
            print("[feishu] 未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，跳过飞书机器人")
            return
        self._ensure_clients(app_id, app_secret)
        threading.Thread(target=self._run_ws_loop, daemon=True, name="feishu-ws").start()
        print("[feishu] 飞书机器人已启动（长连接）")

    def _run_ws_loop(self) -> None:
        """在独立线程内自建事件循环，并在同线程创建并启动 WS client。"""
        import asyncio
        import lark_oapi.ws.client as ws_client_mod
        app_id, app_secret = _load_feishu_config()
        if not app_id or not app_secret:
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # SDK 内部持有模块级 loop，切换成当前线程自建的 loop 才能稳定长连接
        ws_client_mod.loop = loop
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_message)
                   .build())
        self._ws_client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )
        try:
            self._ws_client.start()
        except Exception as exc:
            print(f"[feishu] ws loop exit: {exc}")

    def _send_text(self, receive_id: str, receive_id_type: str, text: str):
        """发送文本消息（自动截断超长文本）。"""
        body = (CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(json.dumps({"text": text[: _TEXT_MAX]}, ensure_ascii=False))
                .build())
        req = (CreateMessageRequest.builder()
               .receive_id_type(receive_id_type)
               .request_body(body)
               .build())
        resp = self._api_client.im.v1.message.create(req)
        if resp.code != 0:
            print(f"[feishu] send text failed: code={resp.code} msg={resp.msg}")

    def _send_markdown(self, receive_id: str, receive_id_type: str, markdown: str):
        """以飞书互动卡片（markdown 元素）发送 Markdown 内容，让飞书按卡片规则解析。
        注意：content 必须传 JSON.stringify 后的字符串（内部 \n 为真换行），否则会被再次转义破坏格式。"""
        content = markdown[:_TEXT_MAX * 2]
        # schema 2.0（elements 放 body 下）：飞书按新版卡片规则渲染 markdown，支持表格/列表等
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content},
                ],
            },
        }
        self._post_interactive_card(receive_id, receive_id_type, card)

    def _ensure_tenant_token(self) -> str:
        """获取并缓存飞书应用 tenant_access_token（原始 HTTP 发送互动卡片使用）。"""
        if self._tenant_token:
            return self._tenant_token
        import urllib.request
        app_id, app_secret = _load_feishu_config()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") == 0 and data.get("tenant_access_token"):
            self._tenant_token = data["tenant_access_token"]
        return self._tenant_token

    def _post_interactive_card(self, receive_id: str, receive_id_type: str, card: dict):
        """原始 HTTP 发送互动卡片：content 传 JSON 字符串，避免 SDK 二次序列化导致换行丢失破坏 markdown。"""
        try:
            import urllib.request
            from urllib.parse import urlencode
            token = self._ensure_tenant_token()
            if not token:
                print("[feishu] interactive card send skipped: missing tenant token")
                return
            content = json.dumps(card, ensure_ascii=False)
            url = "https://open.feishu.cn/open-apis/im/v1/messages?" + urlencode({"receive_id_type": receive_id_type})
            payload = json.dumps({"receive_id": receive_id, "msg_type": "interactive", "content": content},
                                 ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer " + token},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") != 0:
                print(f"[feishu] interactive card failed: code={result.get('code')} msg={result.get('msg')}")
        except Exception as exc:
            print(f"[feishu] interactive card failed: {exc}")


    def _upload_and_send_image(self, receive_id: str, receive_id_type: str, png_bytes: bytes) -> bool:
        """上传 PNG 拿到 image_key 后发送图片消息；失败返回 False。"""
        try:
            body = (CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(png_bytes))
                    .build())
            img_req = (CreateImageRequest.builder()
                       .request_body(body)
                       .build())
            img_resp = self._api_client.im.v1.image.create(img_req)
            if img_resp.code != 0:
                print(f"[feishu] upload image failed: code={img_resp.code} msg={img_resp.msg}")
                return False
            image_key = getattr(img_resp.data, "image_key", "") or ""
            if not image_key:
                return False
            body = (CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("image")
                    .content(json.dumps({"image_key": image_key}))
                    .build())
            req = (CreateMessageRequest.builder()
                   .receive_id_type(receive_id_type)
                   .request_body(body)
                   .build())
            resp = self._api_client.im.v1.message.create(req)
            return resp.code == 0
        except Exception as exc:
            print(f"[feishu] send image failed: {exc}")
            return False


    def _run_query(self, cid: str, request_id: str, text: str):
        """后台执行 LangGraph（流式）并更新会话落盘，返回 (answer, thinking, executed_sql, error)。

        通过 StreamBus + ACTIVE_REQUESTS 镜像，把飞书查询的思考过程/token 实时写到
        网页端共享状态，网页端在对应会话上即可看到动态思考，而不是只显示「处理中...」。
        """
        topic_id = self.sessions[cid]["topic_id"]
        config = {"configurable": {"thread_id": cid}}
        state_input = {
            "conversation_id": cid,
            "topic_id": topic_id,
            "request_id": request_id,
            "current_user_input": text,
        }
        # 飞书请求也绑定日志上下文，方便在 langgraph_app.jsonl 中按 request_id 排查
        context_token = bind_log_context(
            conversation_id=cid,
            topic_id=topic_id,
            request_id=request_id,
            graph_thread_id=cid,
        )
        start_wall = time.time()
        try:
            # 正常链路：占位落盘 → 流式执行 → 用最终状态覆盖占位
            _now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self.sessions[cid]["messages"].append({
                "role": "user", "content": text, "request_at": _now_str,
            })
            self.sessions[cid]["messages"].append({
                "role": "assistant", "content": "", "sql": "", "thinking": "",
                "request_id": request_id, "thinking_seconds": 0,
                "status": "processing", "error_message": "", "llm_tokens": {},
                "request_at": _now_str,
            })
            _persist(self.sessions, cid)
            # 注册为进行中请求：飞书会话也能在网页端轮询到实时思考/回答
            self.active_requests[cid] = {
                "request_id": request_id,
                "status": "AI 正在思考...",
                "thinking_parts": [],
                "thinking": "",
                "content": "",
                "sql": "",
                "llm_tokens": {},
                "context": None,
            }
            bus = StreamBus(sink=make_active_sink(self.active_requests, cid, request_id))
            # 后台线程绑定流式总线，LLM 层的 thinking/answer token 才会实时镜像到注册表
            bind_stream_bus(bus)
            thinking_parts = ["[intent] query"]
            seen = set()          # 出现过哪些节点（用于旧链路 SQL 判定）
            node_seq = {}         # 节点出现次数：0 行自愈等回环时展示为 node#2 区分轮次
            error = ""
            answer = ""
            thinking = ""
            display_sql = ""
            try:
                for chunk in self.app.stream(state_input, config, subgraphs=True):
                    node_dict = chunk[1] if isinstance(chunk, tuple) else chunk
                    for node_name, node_update in node_dict.items():
                        if not node_name:
                            continue
                        # 折叠 execute_query 工具内部 Seeker 执行链节点，不当作主流程步骤打扰用户
                        if node_name in _SEEKER_INTERNAL_NODES:
                            seen.add(node_name)
                            continue
                        seen.add(node_name)
                        seq = node_seq.get(node_name, 0) + 1
                        node_seq[node_name] = seq
                        display_node = node_name if seq == 1 else f"{node_name}#{seq}"
                        label = NODE_LABELS.get(node_name, node_name)
                        detail = _extract_node_detail(node_name, node_update)
                        display_text = label + ("\n" + detail if detail else "")
                        thinking_parts.append("[" + display_node + "] " + display_text)
                        # 实时写入共享快照，网页端 /api/chat/status 立即可以读
                        bus.emit({"type": "thinking", "node": display_node, "text": display_text})

                final_state = self.app.get_state(config)
                result = (final_state and final_state.values) or {}
                final_answer = str(result.get("final_answer") or result.get("respond_text") or "")
                answer = final_answer
                generated_sql = result.get("generated_sql", "")
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
                    # 兜底：旧链路（generated_sql + 节点判定）无 executed_sql 时沿用展示
                    sql_query_nodes = {
                        "retrieve_schema",
                        "generate_sql",
                        "validate_sql",
                        "prepare_sql_fix",
                        "execute_sql",
                        "prepare_sql_exec_fix",
                        "persist_result",
                    }
                    display_sql = generated_sql if bool(seen & sql_query_nodes) else ""
                thinking = "\n".join(thinking_parts)
                llm_tokens = get_llm_token_usage()
                self.sessions[cid]["messages"][-1] = {
                    "role": "assistant", "content": answer, "sql": display_sql,
                    "thinking": thinking, "request_id": request_id,
                    "thinking_seconds": round(time.time() - start_wall),
                    "status": "success", "error_message": "",
                    "llm_tokens": llm_tokens,
                    "request_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                }
                _persist(self.sessions, cid)
                bus.emit({
                    "type": "done", "content": answer, "sql": display_sql,
                    "thinking": thinking, "llm_tokens": llm_tokens,
                })
                return answer, thinking, display_sql, error
            except Exception as exc:
                error = str(exc)
                thinking = "\n".join(thinking_parts)
                error_id = uuid.uuid4().hex
                self.sessions[cid]["messages"][-1] = {
                    "role": "assistant", "content": "", "sql": "",
                    "thinking": thinking, "request_id": request_id,
                    "thinking_seconds": round(time.time() - start_wall),
                    "status": "failed", "error_message": error,
                    "llm_tokens": {},
                    "request_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                }
                _persist(self.sessions, cid)
                bus.emit({
                    "type": "error",
                    "text": "系统暂时无法完成本次查询，请稍后重试。",
                    "error_id": error_id,
                })
                return answer, thinking, display_sql, error
        finally:
            reset_log_context(context_token)
            # 查询结束：从进行中注册表移除，前端轮询据此判定完成并重新加载
            self.active_requests.pop(cid, None)
            try:
                from web.conversation_events import broadcast_conversations
                broadcast_conversations()
            except Exception:
                pass
            # bus 已完成镜像，最后关闭只影响队列，active_requests 快照已保留
            if "bus" in locals():
                bus.close()

    def _build_reply_text(self, answer: str, cid: str) -> str:
        """组装飞书回复文本：去掉 chart 块与本地保存路径，保留 Markdown 正文。"""
        answer = re.sub(r"```\s*chart\s*\n.*?```", "", answer, flags=re.S)
        # 网页端会显示保存路径，但飞书端不暴露：整行删除 + 隐藏散落在正文里的本地路径片段
        answer = re.sub(r"完整(明细|数据)?已保存[:：].*", "", answer, flags=re.M)
        answer = re.sub(r"[A-Za-z]:[\\/][^\s,\'\"’”]+", "", answer)
        # 保留段落之间的空行：飞书互动卡片对 Markdown 表格要求表后有空行，
        # 全部去空行会导致表格后的结论句被解析成表格的一部分（网页端较宽松看不出）。
        out_lines = []
        _blank_seen = False
        for _raw in answer.splitlines():
            _line = _raw.strip()
            if not _line:
                # 连续空行只保留一个，避免输出过多空行
                if not _blank_seen and out_lines:
                    out_lines.append("")
                _blank_seen = True
                continue
            out_lines.append(_line)
            _blank_seen = False
        text = "\n".join(out_lines).strip()
        return text or "查询完成，但没有生成可展示的回答。"

    def _log_feishu_event(self, cid: str, request_id: str, event: str, reason: str):
        """把飞书环节（图表发送等）失败原因写入 langgraph_app.jsonl，便于按 request_id 排查。"""
        try:
            from agentTest.langgraph_app.runtime.graph_logger import _write_log
            import logging
            _write_log(
                logging.INFO, event, node_name="feishu",
                conversation_id=cid, request_id=request_id, reason=reason,
            )
        except Exception:
            pass

    def _try_send_chart(self, cid: str, request_id: str, answer: str, receive_id: str, receive_id_type: str) -> bool:
        """优先用回答里的 chart 块，否则从落盘结果自动选图，渲染 PNG 后发送。"""
        try:
            from web.chart_png import extract_chart_spec_from_answer, render_spec_png
            spec = extract_chart_spec_from_answer(answer)
            if not spec:
                from agentTest.langgraph_app.tools.chart_tool import build_charts_for_request
                specs, err = build_charts_for_request(cid, request_id, type="auto")
                if err or not specs:
                    self._log_feishu_event(cid, request_id, "feishu.chart.skipped", f"no chart spec / 落盘结果不可用: {err or ''}")
                    return False
                spec = specs[0]
            png_bytes = render_spec_png(spec)
            ok = self._upload_and_send_image(receive_id, receive_id_type, png_bytes)
            if not ok:
                self._log_feishu_event(cid, request_id, "feishu.chart.skipped", "图片上传或发送失败，请检查 im:resource 权限与日志输出")
            return ok
        except Exception as exc:
            self._log_feishu_event(cid, request_id, "feishu.chart.skipped", f"异常: {exc}")
            print(f"[feishu] chart send skipped: {exc}")
            return False

    def _resolve_user_name(self, open_id: str) -> str:
        """通过通讯录 open_id 反查用户姓名；无权限/失败时回退 open_id。"""
        try:
            req = (GetUserRequest.builder()
                   .user_id_type("open_id")
                   .user_id(open_id)
                   .build())
            resp = self._api_client.contact.v3.user.get(req)
            if resp.code != 0:
                # 通常是缺少通讯录读取权限，打印出来方便在控制台/日志中排查
                print(f"[feishu] resolve user name api error: code={resp.code} msg={resp.msg}")
                return open_id
            if resp.data and resp.data.user:
                name = resp.data.user.name or ""
                if name.strip():
                    return name.strip()
        except Exception as exc:
            print(f"[feishu] resolve user name failed: {exc}")
        return open_id


    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """飞书收到新消息回调（ws 事件线程内调用，查询放到后台线程避免阻塞长连接）。"""
        try:
            message = getattr(data.event, "message", None)
            sender = getattr(data.event, "sender", None)
            if message is None or sender is None:
                return
            if message.message_type != "text":
                return
            raw = str(message.content or "")
            chat_type = str(message.chat_type or "p2p")
            # 群聊只在 @ 机器人时回复
            if chat_type != "p2p" and not _is_group_mention(raw):
                return
            open_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or ""
            if not open_id:
                return
            user_text = _parse_text_content(raw)
            if not user_text:
                return
            # 私聊回给用户 open_id；群聊回给群 chat_id
            receive_id = open_id if chat_type == "p2p" else str(message.chat_id or "")
            receive_id_type = "open_id" if chat_type == "p2p" else "chat_id"
            if not receive_id:
                return

            user_name = self._resolve_user_name(open_id)
            cid = _collect_messages(self.sessions, open_id, user_name)
            request_id = uuid.uuid4().hex
            # 先回占位提示，用户立即感知已接收；查询完成后另发结果
            self._send_text(receive_id, receive_id_type, f"正在查询中…（request_id: {request_id}）")
            threading.Thread(
                target=self._process_message,
                args=(cid, request_id, user_text, receive_id, receive_id_type),
                daemon=True,
                name="feishu-query",
            ).start()
        except Exception as exc:
            print(f"[feishu] handle message failed: {exc}")

    def _process_message(self, cid: str, request_id: str, text: str, receive_id: str, receive_id_type: str):
        """后台完成查询后发送 文字摘要 + PNG 图片。"""
        try:
            answer, thinking, executed_sql, error = self._run_query(cid, request_id, text)
            if error:
                error_id = uuid.uuid4().hex
                self._send_text(receive_id, receive_id_type,
                                f"系统暂时无法完成本次查询，请稍后重试。\n错误编号：{error_id}")
                return
            reply = self._build_reply_text(answer, cid)
            self._send_markdown(receive_id, receive_id_type, reply)
            # 数值型结果统一补发一张静态 PNG 图：优先用回答里的 chart 块，没有则从落盘结果自动选图
            self._try_send_chart(cid, request_id, answer, receive_id, receive_id_type)
        except Exception as exc:
            print(f"[feishu] process message failed: {exc}")
            self._send_text(receive_id, receive_id_type, "查询过程中出现异常，请稍后重试。")


def start_feishu_bot(runtime, app, sessions, active_requests) -> FeishuBot:
    """web/server.py 启动时调用：创建并后台运行飞书机器人。"""
    bot = FeishuBot(runtime, app, sessions, active_requests)
    bot.start()
    return bot
