// 智能数仓助手 - 前端（流式 + 打分 + 重命名/删除）
var API = "/api";
// 当前选中的完整前端对话标识
var conversationId = null;
var conversations = {};
// 创建人（仅用于区分不同用户的对话，测试用），localStorage 持久化
var creator = localStorage.getItem("creator") || "";
// 初始会话引导只执行一次：防止命名弹层被重复触发（连点/双击/回车+点击）创建多个会话
var _bootstrapDone = false;
// 是否为手动“设置称呼”入口打开的弹层（决定取消/留空行为）
var _nameOverlayManual = false;
// 每个会话的进行中请求状态（conversationId -> {thinking,status,content,...}），
// 切换会话后占位消息与会话绑定，不共享同一个进度条
var pendingRequests = {};
// 刷新/切回后的进行中请求恢复：轮询定时器与重入保护（conversationId -> 标志）
var activePollers = {};
var resuming = {};
// 左侧会话列表的进行中状态：conversation_id -> { active, last_status, pendingDone }
// pendingDone 表示该会话已完成但用户尚未点击查看，用绿色小圆点提示
var convActivity = {};
var _convPolling = false; // 会话状态轮询是否正在执行，避免并发重叠

function persistCurrentConversation() {
    // 切换/新建/删除会话时记录当前会话到 localStorage，刷新后恢复用户原本所在会话
    if (conversationId) localStorage.setItem("currentConvId", conversationId);
    else localStorage.removeItem("currentConvId");
}

function $(id) { return document.getElementById(id); }
function hideEmpty() { var el = $("emptyState"); if (el) el.style.display = "none"; }

function scrollToBottom(el, force) {
    // 高频滚动合并：逐字流式一帧只滚一次，避免 smooth 动画反复打断造成抖动/滞后
    if (!el) return;
    if (!force && el._userScrolledUp) return;
    if (el._scrollRafId) return;
    el._scrollRafId = requestAnimationFrame(function () {
        el._scrollRafId = null;
        el.scrollTop = el.scrollHeight;
    });
}


function stickBottom(el, prevScrollHeight) {
    // 流式内容增长时增量跟随：视口原本贴底时，让 scrollTop 随内容高度同步增长，避免换行时先上移再跳回的生硬感
    if (!el) return;
    if (el._userScrolledUp) return;
    var delta = el.scrollHeight - (prevScrollHeight != null ? prevScrollHeight : el.scrollHeight);
    if (delta > 0) el.scrollTop += delta;
}

async function newChat() {
    try {
        var res = await fetch(API + "/conversations", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ creator: creator }),
        });
        var data = await res.json();
        if (data.creator) creator = data.creator;
        conversationId = data.conversation_id;
        persistCurrentConversation();
        conversations[conversationId] = { title: "新对话", creator: creator, messages: [], _loadedFromServer: true };
        $("chatArea").innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
        refreshConvList();
        updateInputLock();
        updateContextRing(null, null);
        return true;
    } catch (e) { return false; }
}

async function loadConversation(conversationIdToLoad, opts) {
    conversationId = conversationIdToLoad;
    persistCurrentConversation();
    // 点击进入会话 → 取消“完成待查看”绿色标记
    clearConversationDone(conversationIdToLoad);
    var conv = conversations[conversationIdToLoad];
    // 总是从服务端拉取最新对话：飞书/后台会话可能在当前页面之外追加了新的
    // 进行中或最终消息，仅命中本地缓存会导致必须手动刷新才能看到新内容。
    try {
        var res = await fetch(API + "/conversations/" + conversationIdToLoad);
        if (res.ok) {
            var data = await res.json();
            conversations[conversationIdToLoad] = {
                title: data.title || "新对话",
                creator: data.creator || "",
                messages: data.messages || [],
                // 会话级上下文占用：服务端从 Checkpoint 实时估算，不随消息落库
                _context: data.context || null,
                _loadedFromServer: true,
            };
        }
    } catch (e) {}
    // 服务端拉取失败时退回本地缓存，避免整个页面不可用
    conv = conversations[conversationIdToLoad];
    // 等待拉取期间用户已切到其他会话：丢弃过期加载，避免用旧会话覆盖当前界面与上下文圆环
    if (conversationId !== conversationIdToLoad) return;
    var area = $("chatArea");
    area.innerHTML = "";
    if (conv && conv.messages && conv.messages.length > 0) {
        // 刷新/重启后恢复的"处理中"占位轮：非 skipResume 时跳过其静态渲染（可能被恢复逻辑改成动态占位），
        // 避免"处理中...（若一直未完成）"静态提示与动态占位同时出现
        var _lastMsg = conv.messages[conv.messages.length - 1];
        var _skipStub = false;
        if (!(opts && opts.skipResume) && _lastMsg
                && _lastMsg.role === "assistant" && _lastMsg.status === "processing") {
            _skipStub = true;
        }
        conv.messages.forEach(function (m, idx) {
            if (_skipStub && idx === conv.messages.length - 1) return;
            appendMessage(m.role, m.content, m.sql, m.thinking, m.evaluator, m.dialogue_id, m.request_id, m.thinkingOpen, m);
        });
    } else {
        area.innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
    }
    // 该会话仍有进行中请求时恢复占位消息（思考内容从状态对象读取）
    if (pendingRequests[conversationIdToLoad]) appendPendingMessage(conversationIdToLoad);
    refreshConvList();
    updateInputLock();
    // 上下文圆环用服务端从 Checkpoint 实时估算的占用恢复（不同会话各自独立，不依赖落盘快照）
    updateContextRing(conv && conv._context, null);
    // 最新轮次为"处理中"（刷新/重启后从 MySQL 恢复）时，尝试从后台恢复实时状态（仿 codex）
    if (!(opts && opts.skipResume)) {
        var _msgs = conv && conv.messages;
        var _last = _msgs && _msgs[_msgs.length - 1];
        if (_last && _last.role === "assistant" && _last.status === "processing") {
            maybeResumeActiveRequest(conversationIdToLoad);
        }
    }
}

async function renameConv(conversationIdToRename, event) {
    event.stopPropagation();
    var name = prompt("新名称：", conversations[conversationIdToRename] ? conversations[conversationIdToRename].title : "");
    if (!name || !name.trim()) return;
    try {
        await fetch(API + "/conversations/" + conversationIdToRename, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: name.trim() }),
        });
        if (conversations[conversationIdToRename]) conversations[conversationIdToRename].title = name.trim();
        refreshConvList();
    } catch (e) {}
}

async function deleteConv(conversationIdToDelete, event) {
    event.stopPropagation();
    if (!confirm("确定删除此对话？")) return;
    try {
        await fetch(API + "/conversations/" + conversationIdToDelete, { method: "DELETE" });
        var pendToDelete = pendingRequests[conversationIdToDelete];
        if (pendToDelete) flushAnswerTypewriter(pendToDelete);
        delete pendingRequests[conversationIdToDelete];
        delete conversations[conversationIdToDelete];
        if (conversationId === conversationIdToDelete) {
            conversationId = null;
            persistCurrentConversation();
            $("chatArea").innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
        }
        refreshConvList();
        updateInputLock();
    } catch (e) {}
}

function computeConvStatus(c) {
    // 合并服务端 active/last_status 与本地的 pendingDone 标记
    var prev = convActivity[c.conversation_id] || { active: false, last_status: "", pendingDone: false };
    var activeNow = !!c.active;
    var lastStatusNow = c.last_status || "";
    var pendingDone = prev.pendingDone || false;
    if (!activeNow && prev.active && (lastStatusNow === "success" || lastStatusNow === "chat")) {
        // 上一轮还在处理、这一轮已完成且是成功/普通聊天 → 显示绿色待查看标记
        pendingDone = true;
    }
    if (activeNow) pendingDone = false; // 仍在处理中：保留加载图标，不显示绿色
    if (!prev.active && activeNow) pendingDone = false; // 刚开始处理：也不显示绿色
    convActivity[c.conversation_id] = { active: activeNow, last_status: lastStatusNow, pendingDone: pendingDone };
    return { active: activeNow, pendingDone: pendingDone };
}

function renderConvStatus(div, status) {
    // 更新某个会话条目的状态标记：处理中=加载圈，完成未点击=绿色圆点
    var old = div.querySelector(".conv-status");
    if (old) old.remove();
    var el = document.createElement("span");
    el.className = "conv-status";
    if (status.active) {
        el.classList.add("spinner");
        el.title = "处理中...";
    } else if (status.pendingDone) {
        el.classList.add("done");
        el.title = "查询已完成，点击查看";
    } else {
        return;
    }
    div.insertBefore(el, div.firstChild);
}

function clearConversationDone(convId) {
    // 用户点击进入某会话（或该会话在当前页内已完成自动刷新）时，取消绿色待查看标记
    if (convActivity[convId]) {
        convActivity[convId].pendingDone = false;
        var div = document.querySelector(".conv-item[data-conv-id=\"" + convId + "\"]");
        if (div) renderConvStatus(div, { active: !!convActivity[convId].active, pendingDone: false });
    }
}

async function pollConvActivity() {
    // 定时轮询会话列表：动态刷新左侧状态图标（处理中/完成待查看），不整表重建避免打断操作
    if (_convPolling) return;
    _convPolling = true;
    try {
        var res = await fetch(API + "/conversations");
        var data = await res.json();
        data.conversations.forEach(function (c) {
            var status = computeConvStatus(c);
            var div = document.querySelector(".conv-item[data-conv-id=\"" + c.conversation_id + "\"]");
            if (div) renderConvStatus(div, status);
        });
    } catch (e) {}
    finally { _convPolling = false; }
}

async function refreshConvList() {
    var list = $("convList"); if (!list) return;
    try {
        var res = await fetch(API + "/conversations");
        var data = await res.json();
        list.innerHTML = "";
        data.conversations.forEach(function (c) {
            conversations[c.conversation_id] = conversations[c.conversation_id] || { title: c.title, creator: c.creator, messages: [] };
            conversations[c.conversation_id].creator = c.creator || "";
            var div = document.createElement("div");
            div.setAttribute("data-conv-id", c.conversation_id);
            div.className = "conv-item" + (c.conversation_id === conversationId ? " active" : "");
            div.onclick = function () { loadConversation(c.conversation_id); };

            // 标题 + 创建人（小字）放在同一列，创建人用于区分不同用户的对话
            var mainCol = document.createElement("div");
            mainCol.style.flex = "1";
            mainCol.style.overflow = "hidden";
            var span = document.createElement("span");
            span.textContent = c.title || "新对话";
            span.style.display = "block";
            span.style.overflow = "hidden";
            span.style.textOverflow = "ellipsis";
            span.style.whiteSpace = "nowrap";
            mainCol.appendChild(span);
            if (c.creator) {
                var cr = document.createElement("span");
                cr.className = "conv-creator";
                cr.textContent = "by " + c.creator;
                mainCol.appendChild(cr);
            }
            div.appendChild(mainCol);

            var actions = document.createElement("span");
            actions.className = "conv-actions";

            var renBtn = document.createElement("button");
            renBtn.className = "conv-action-btn";
            renBtn.textContent = "✎";
            renBtn.title = "重命名";
            renBtn.onclick = function (e) { renameConv(c.conversation_id, e); };
            actions.appendChild(renBtn);

            var delBtn = document.createElement("button");
            delBtn.className = "conv-action-btn";
            delBtn.textContent = "✕";
            delBtn.title = "删除";
            delBtn.onclick = function (e) { deleteConv(c.conversation_id, e); };
            actions.appendChild(delBtn);

            div.appendChild(actions);
            // 渲染会话状态图标（处理中/完成待查看），后续由 pollConvActivity 增量更新
            renderConvStatus(div, computeConvStatus(c));
            list.appendChild(div);
        });
    } catch (e) {}
}

function lockInput(disabled) {
    var inp = $("msgInput"), btn = $("sendBtn");
    if (inp) inp.disabled = disabled;
    if (btn) btn.disabled = disabled;
}

function updateInputLock() {
    // 只有当前显示会话存在未结束请求（done/error 未收）时才锁定输入
    var pend = pendingRequests[conversationId];
    lockInput(!!(pend && !pend.doneReceived));
}

function createPendingRequest() {
    // 占位消息状态：思考文本/状态/最终回复字段，切走再切回也能恢复
    return {
        thinking: "", status: "AI 正在思考...",
        content: "", sql: "", evaluator: null, dialogue_id: 0, request_id: "",
        request_at: "",  // 本轮消息发送时间（随 MySQL 落盘）
        thinkingOpen: true,  // 思考面板默认展开，用户折叠/展开后保持
        thinkingParts: [],  // 思考按流式段落存储（sid -> 文本），支持最终回复回收
        answerQueue: [],  // 最终回答重放 token 的打字机队列
        answerTimer: null,  // 打字机定时器句柄
        answerStartAt: 0,  // 打字机启动时间，用于动态调速
        answerHardTimer: null,  // 硬上限兜底定时器，避免输入框长期锁定
        pendingFinalContent: null,  // done 提前到达时暂存的最终内容
        finalizeAfterTypewriter: false,  // 请求结束后等待打字机播完再保存消息
        doneReceived: false,  // 本轮业务已结束（收到 done/error），输入框可解锁
        thinkStartAt: 0,  // 思考计时起点（ms）
        thinkTimer: null,  // 思考计时器句柄
        thinkingSeconds: 0,  // 思考总耗时（秒），固化时保存
        llmTokens: null,  // 本轮 LLM token 消耗汇总（输入/输出/缓存命中/未命中）
        contextProgress: null,  // 上下文使用进度（used/window/percent），供进度条展示
        contextCompacted: null,  // 上下文压缩提示（saved_chars 等），压缩发生时展示
    };
}

async function maybeResumeActiveRequest(convId) {
    // 刷新/切回后检测后台是否仍有进行中请求：有则立即展示快照并轮询，无则重载一次拿最终结果
    if (resuming[convId]) return;
    resuming[convId] = true;
    try {
        var res = await fetch(API + "/chat/status/" + convId);
        var data = await res.json();
        if (data && data.active && data.request_id) {
            var pend = pendingRequests[convId] || (pendingRequests[convId] = createPendingRequest());
            pend.request_id = data.request_id;
            pend.thinking = data.thinking || "";
            pend.status = data.status || pend.status;
            pend.content = data.content || "";
            pend.contextProgress = data.context || null;
            if (conversationId === convId) {
                appendPendingMessage(convId);
                updatePendingMessage(convId);
                updateContextRing(data.context || null, null);
            }
            startPollingActive(convId);
            // 恢复进行中请求后锁定输入，避免该会话并发二次请求
            updateInputLock();
        } else {
            // 后台已无进行中请求：重新加载一次（worker 可能已完成落盘）
            await loadConversation(convId, { skipResume: true });
        }
    } catch (e) {}
    finally { resuming[convId] = false; }
}

function startPollingActive(convId) {
    // 启动对后台进行中请求的轮询（已存在则跳过）
    if (activePollers[convId]) return;
    activePollers[convId] = 1;
    setTimeout(function () { pollActive(convId); }, 1200);
}

function stopPollingActive(convId) {
    // 停止轮询
    delete activePollers[convId];
}

async function pollActive(convId) {
    // 轮询后台进行中快照：更新占位内容；完成/结束时重载拿最终结果
    if (!activePollers[convId]) return;
    var data;
    try {
        var res = await fetch(API + "/chat/status/" + convId);
        data = await res.json();
    } catch (e) {
        stopPollingActive(convId);
        return;
    }
    if (!data || !data.active) {
        // 后台已完成/结束：停止轮询，重新加载拿最终结果
        stopPollingActive(convId);
        delete pendingRequests[convId];
        await loadConversation(convId, { skipResume: true });
        return;
    }
    var pend = pendingRequests[convId] || (pendingRequests[convId] = createPendingRequest());
    pend.request_id = data.request_id || pend.request_id;
    pend.thinking = data.thinking || "";
    pend.status = data.status || pend.status;
    pend.content = data.content || "";
    pend.contextProgress = data.context || pend.contextProgress;
    // 恢复进行中请求时补消息时间（只补一次，避免轮询反复刷新）
    if (!pend.request_at) pend.request_at = data.request_at || fmtNow();
    // 完成态：停止轮询并重载（拿到落盘的最终答案）
    if (data.status === "done" || data.status === "error") {
        stopPollingActive(convId);
        delete pendingRequests[convId];
        await loadConversation(convId, { skipResume: true });
        return;
    }
    if (conversationId === convId) {
        updatePendingMessage(convId);
        updateContextRing(data.context || null, null);
    }
    // 进行中保持输入锁定（占位可能刚由轮询创建）
    updateInputLock();
    setTimeout(function () { pollActive(convId); }, 1200);
}

async function sendMsg() {
    var input = $("msgInput"); if (!input) return;
    var msg = input.value.trim(); if (!msg) return;

    if (!conversationId) {
        lockInput(true);
        var ok = await newChat();
        if (!ok) { appendMessage("assistant", "无法连接服务器，请确认已启动: python web/server.py"); updateInputLock(); return; }
    }

    var reqConv = conversationId;  // 绑定发起请求的会话，异步期间切换会话不会串台
    var pendOld = pendingRequests[reqConv];
    if (pendOld && pendOld.doneReceived) {
        // 上一条业务已结束（done 已收）但消息未固化：先固化旧消息，再发起新请求
        flushAnswerTypewriter(pendOld);
        finalizePendingRequest(reqConv, pendOld);
    }
    if (pendingRequests[reqConv]) return;  // 该会话已有进行中请求，不重复发送

    var conv = conversations[reqConv];
    if (conv && (!conv.title || conv.title === "新对话")) conv.title = msg.slice(0, 40);

    input.value = "";
    // 占位消息状态：思考文本/状态/最终回复字段，切走再切回也能恢复
    pendingRequests[reqConv] = createPendingRequest();
    lockInput(true);
    hideEmpty();
    var userMsgObj = { role: "user", content: msg, request_at: fmtNow() };
    appendMessage("user", msg, "", "", null, 0, "", undefined, userMsgObj);
    if (conv) conv.messages.push(userMsgObj);
    pendingRequests[reqConv].request_at = userMsgObj.request_at;
    appendPendingMessage(reqConv);

    console.log("[sendMsg] user=" + msg.slice(0, 60) + " conversation=" + reqConv);

    try {
        var res = await fetch(API + "/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ conversation_id: reqConv, message: msg }),
        });

        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";

        while (true) {
            var read = await reader.read();
            if (read.done) break;
            buffer += decoder.decode(read.value, { stream: true });

            var lines = buffer.split("\n");
            buffer = lines.pop() || "";
            for (var k = 0; k < lines.length; k++) {
                var line = lines[k];
                if (!line.startsWith("data: ")) continue;
                try {
                    var event = JSON.parse(line.slice(6));
                    var pend = pendingRequests[reqConv]; if (!pend) continue;
                    if (event.request_id) pend.request_id = event.request_id;
                    if (event.type === "status" || event.type === "thinking") {
                        // 状态显示第一行（节点标签），思考面板按段落累积
                        var firstLine = String(event.text || "").split("\n")[0];
                        if (firstLine) pend.status = firstLine;
                        if (event.text) {
                            pend.thinkingParts.push({ sid: null, text: event.text });
                            rebuildThinking(pend);
                        }
                        updatePendingMessage(reqConv);
                    } else if (event.type === "token") {
                        // 思考/回答逐字流式增量：思考按流式段落追加，回答追加到预览区
                        if (event.scope === "answer") {
                            // 最终回答开始流式：停止思考计时并切换状态
                            if (pend.thinkTimer) stopThinkTimer(pend);
                            if (pend.status === "AI 正在思考..." || pend.status.indexOf("思考") >= 0) {
                                pend.status = "正在生成回答";
                            }
                            if (event.live === true) {
                                // 实时 token：直接追加
                                pend.content += event.text;
                            } else {
                                // 重放 token：进入打字机队列逐字展示
                                pend.answerQueue.push(event.text);
                                startAnswerTypewriter(pend, reqConv);
                            }
                        } else {
                            var sid = event.stream_id || "";
                            var part = null;
                            for (var pi = pend.thinkingParts.length - 1; pi >= 0; pi--) {
                                if (pend.thinkingParts[pi].sid === sid) {
                                    part = pend.thinkingParts[pi];
                                    break;
                                }
                            }
                            if (part) {
                                part.text += event.text;
                            } else {
                                pend.thinkingParts.push({ sid: sid, text: event.text });
                            }
                            rebuildThinking(pend);
                        }
                        updatePendingMessage(reqConv);
                    } else if (event.type === "thinking_retract") {
                        // 最终回复从思考面板移除，改由回答区逐字展示
                        var rsid = event.stream_id || "";
                        pend.thinkingParts = pend.thinkingParts.filter(function (p) { return p.sid !== rsid; });
                        rebuildThinking(pend);
                        updatePendingMessage(reqConv);
                    } else if (event.type === "context_progress") {
                        // 上下文使用进度：更新进度条（used / window / percent）
                        pend.contextProgress = {
                            used_tokens: event.used_tokens,
                            window_tokens: event.window_tokens,
                            percent: event.percent,
                        };
                        // 同步到会话内存对象：同一会话内切走再切回仍能恢复圆环（不依赖服务端拉取）
                        if (conversations[reqConv]) conversations[reqConv]._context = pend.contextProgress;
                        updatePendingMessage(reqConv);
                        // 输入框左侧圆环实时更新（仅当前显示会话）
                        if (reqConv === conversationId) updateContextRing(pend.contextProgress, pend.contextCompacted);
                    } else if (event.type === "context_compacted") {
                        // 上下文压缩提示：展示"已压缩"与节省量
                        pend.contextCompacted = {
                            before_chars: event.before_chars,
                            after_chars: event.after_chars,
                            saved_chars: event.saved_chars,
                        };
                        updatePendingMessage(reqConv);
                        if (reqConv === conversationId) updateContextRing(pend.contextProgress, pend.contextCompacted);
                    } else if (event.type === "done") {
                        pend.doneReceived = true;
                        if (pend.thinkTimer) stopThinkTimer(pend);
                        if (pend.answerTimer) {
                            // 打字机未播完：暂存最终内容，并设 5 秒硬上限兜底固化消息
                            pend.pendingFinalContent = event.content;
                            pend.answerHardTimer = setTimeout(function () {
                                flushAnswerTypewriter(pend);
                                finalizePendingRequest(reqConv, pend);
                            }, 5000);
                        } else {
                            pend.content = event.content;
                        }
                        console.log("[sendMsg] done: content=" + (pend.content || "").slice(0, 80));
                        pend.sql = event.sql;
                        pend.evaluator = event.evaluator;
                        pend.dialogue_id = event.dialogue_id || 0;
                        pend.llmTokens = event.llm_tokens || null;
                        if (event.request_at) pend.request_at = event.request_at;
                    } else if (event.type === "error") {
                        pend.doneReceived = true;
                        if (pend.thinkTimer) stopThinkTimer(pend);
                        flushAnswerTypewriter(pend);
                        var errorIdText = event.error_id
                            ? "\n错误编号：" + event.error_id
                            : "";
                        pend.content = event.text + errorIdText;
                        if (event.request_at) pend.request_at = event.request_at;
                        console.log(
                            "[sendMsg] request failed, error_id="
                            + (event.error_id || "")
                        );
                    }
                } catch (parseErr) {}
            }
        }
    } catch (e) {
        var pend = pendingRequests[reqConv];
        if (pend) {
            pend.doneReceived = true;
            flushAnswerTypewriter(pend);
            pend.content = "连接失败，请确认服务已启动。";
        }
    }

    var pend = pendingRequests[reqConv];
    if (pend && pend.answerTimer) {
        // 打字机仍在播放：等队列耗尽后由 drain 完成收尾，避免截断逐字效果
        pend.finalizeAfterTypewriter = true;
        // done 已收时先解锁输入框，消息固化仍等打字机播完
        updateInputLock();
    } else {
        if (pend) finalizePendingRequest(reqConv, pend);
        updateInputLock();
        refreshConvList();
    }
}

function finalizePendingRequest(convId, pend) {
    // 请求结束时保存最终消息并清理占位（打字机播完后调用）
    var conv = conversations[convId];
    var savedMsg = {
        role: "assistant", content: pend.content || "(无响应)", sql: pend.sql,
        thinking: pend.thinking, evaluator: pend.evaluator, dialogue_id: pend.dialogue_id,
        request_id: pend.request_id, thinkingOpen: pend.thinkingOpen,
        thinkingSeconds: pend.thinkingSeconds || 0,
        llm_tokens: pend.llmTokens || null,
        request_at: pend.request_at || "",
    };
    if (conv) conv.messages.push(savedMsg);
    delete pendingRequests[convId];
    // 只有当前仍显示发起请求的会话时才更新 DOM 与焦点
    if (conversationId === convId) {
        removePendingMessage(convId);
        // 当前会话内已完成：取消该会话的绿色待查看标记（用户已看到结果）
        clearConversationDone(convId);
        console.log("[sendMsg] finalContent=" + (pend.content || "(empty)").slice(0, 100));
        appendMessage("assistant", pend.content || "(无响应)", pend.sql, pend.thinking, pend.evaluator, pend.dialogue_id, pend.request_id, pend.thinkingOpen, savedMsg);
        var inputEl = $("msgInput");
        if (inputEl) inputEl.focus();
    }
    updateInputLock();
    refreshConvList();
}

function rebuildThinking(pend) {
    // 按段落顺序拼接思考面板完整文本
    pend.thinking = pend.thinkingParts.map(function (p) { return p.text; }).join("\n");
}

function startAnswerTypewriter(pend, convId) {
    // 最终回答为整段重放流：递归 setTimeout 逐字追加，模拟逐字输出效果
    if (pend.answerTimer) return;
    pend.answerStartAt = Date.now();
    var tick = function () {
        if (pend.answerQueue.length > 0) {
            pend.content += pend.answerQueue.shift();
            updatePendingMessage(convId);
            // 动态调速：剩余 token 尽量在约 3 秒内播完，避免输入框长期锁定
            var remaining = pend.answerQueue.length;
            var elapsed = Date.now() - pend.answerStartAt;
            var budget = Math.max(0, 3000 - elapsed);
            var delay = remaining > 0 ? Math.max(5, Math.min(40, Math.floor(budget / remaining))) : 40;
            pend.answerTimer = setTimeout(tick, delay);
        } else {
            clearTimeout(pend.answerTimer);
            pend.answerTimer = null;
            if (pend.answerHardTimer) {
                clearTimeout(pend.answerHardTimer);
                pend.answerHardTimer = null;
            }
            // done 提前到达时，队列耗尽后补齐最终内容
            if (pend.pendingFinalContent !== null && pend.pendingFinalContent !== undefined) {
                pend.content = pend.pendingFinalContent;
                pend.pendingFinalContent = null;
                updatePendingMessage(convId);
            }
            // 请求已结束（done 已收）时，由这里完成消息保存
            if (pend.finalizeAfterTypewriter) {
                finalizePendingRequest(convId, pend);
            }
        }
    };
    pend.answerTimer = setTimeout(tick, 25);
}

function flushAnswerTypewriter(pend) {
    // 请求结束/出错时终止打字机与硬上限定时器，避免悬挂
    if (pend.answerTimer) {
        clearTimeout(pend.answerTimer);
        pend.answerTimer = null;
    }
    if (pend.answerHardTimer) {
        clearTimeout(pend.answerHardTimer);
        pend.answerHardTimer = null;
    }
    pend.answerQueue = [];
    // 最终内容已到达时一次性补齐，避免保存截断内容
    if (pend.pendingFinalContent !== null && pend.pendingFinalContent !== undefined) {
        pend.content = pend.pendingFinalContent;
        pend.pendingFinalContent = null;
    }
}

function startThinkTimer(pend) {
    // 思考计时：每秒刷新思考按钮上的已耗时，仿照 codex 展示思考时长
    if (pend.thinkTimer || pend.thinkingSeconds) return;
    pend.thinkStartAt = Date.now();
    pend.thinkTimer = setInterval(function () {
        if (conversationId && pendingRequests[conversationId] === pend) {
            updatePendingMessage(conversationId);
        }
    }, 1000);
}

function stopThinkTimer(pend) {
    // 停止思考计时并定格总耗时（供固化消息展示）
    if (pend.thinkTimer) {
        clearInterval(pend.thinkTimer);
        pend.thinkTimer = null;
    }
    if (pend.thinkStartAt) {
        pend.thinkingSeconds = Math.max(1, Math.round((Date.now() - pend.thinkStartAt) / 1000));
    }
}

function appendPendingMessage(convId) {
    // 在当前会话消息流内追加“AI 正在思考”占位消息（ChatGPT 形式）
    var area = $("chatArea"); if (!area) return;
    if (document.getElementById("pending-msg-" + convId)) return;  // 已存在则不重复追加
    var pend = pendingRequests[convId]; if (!pend) return;

    var wrapper = document.createElement("div"); wrapper.className = "msg assistant";
    wrapper.id = "pending-msg-" + convId;

    var avatar = document.createElement("div"); avatar.className = "avatar";
    avatar.textContent = "AI";

    var bubble = document.createElement("div"); bubble.className = "bubble";
    var spinner = document.createElement("span"); spinner.className = "spinner";
    var statusSpan = document.createElement("span"); statusSpan.className = "pending-status";
    statusSpan.textContent = pend.status;
    bubble.appendChild(spinner); bubble.appendChild(statusSpan);

    // 思考过程面板：置于气泡最上方（ChatGPT 风格），默认展开，用户折叠/展开状态实时记录
    var collapse = document.createElement("div"); collapse.className = "collapse thinking-panel";
    var btn = document.createElement("button"); btn.className = "collapse-btn";
    var content = document.createElement("div"); content.className = "collapse-content pending-thinking";
    content.textContent = pend.thinking;
    // 用户主动上翻查看历史时暂停自动滚动，滚回底部后恢复跟随
    content.addEventListener("scroll", function () {
        content._userScrolledUp = content.scrollTop + content.clientHeight < content.scrollHeight - 40;
    });
    var open = pend.thinkingOpen !== false;
    content.classList.toggle("show", open);
    btn.classList.toggle("open", open);
    btn.innerHTML = '<span class="arrow">' + (open ? "▼" : "▶") + '</span> 查看思考过程';
    btn.onclick = function () {
        var wasOpen = content.classList.contains("show");
        content.classList.toggle("show");
        btn.classList.toggle("open");
        btn.querySelector(".arrow").textContent = wasOpen ? "▶" : "▼";
        pend.thinkingOpen = !wasOpen;
    };
    collapse.appendChild(btn); collapse.appendChild(content);
    bubble.insertBefore(collapse, bubble.firstChild);

    // 最终回答逐字预览区：build_final_answer 的 token 实时显示，done 后由完整回复替换
    var answerPreview = document.createElement("div");
    answerPreview.className = "pending-answer";
    answerPreview.style.display = "none";
    bubble.appendChild(answerPreview);

    // 上下文使用进度改由输入框左侧圆环展示（updateContextRing），不再在消息气泡内渲染

    // 消息区滚动监听只绑定一次：用户上翻历史时暂停自动滚动
    if (!area._scrollBound) {
        area._scrollBound = true;
        area.addEventListener("scroll", function () {
            area._userScrolledUp = area.scrollTop + area.clientHeight < area.scrollHeight - 40;
        });
    }
    wrapper.appendChild(avatar); wrapper.appendChild(bubble);
    area.appendChild(wrapper); scrollToBottom(area, true);
    // 请求未结束且未定格耗时前，启动思考计时
    if (!pend.doneReceived && !pend.thinkingSeconds) startThinkTimer(pend);
}

function updatePendingMessage(convId) {
    // 实时更新当前会话占位消息的状态文案与思考内容
    if (conversationId !== convId) return;
    var pend = pendingRequests[convId]; if (!pend) return;
    var wrapper = document.getElementById("pending-msg-" + convId);
    if (!wrapper) return;
    var area = $("chatArea");
    // 在思考面板与答案更新前记录消息区高度，更新后统一增量跟随，覆盖全部高度增长源
    var prevAreaH = area.scrollHeight;
    var statusEl = wrapper.querySelector(".pending-status");
    if (statusEl) statusEl.textContent = pend.status;
    var thinkEl = wrapper.querySelector(".pending-thinking");
    if (thinkEl) {
        var prevThinkH = thinkEl.scrollHeight;
        thinkEl.textContent = pend.thinking;
        // 思考面板：用户未主动上翻时自动滚动到底部，保证始终看到最新内容
        stickBottom(thinkEl, prevThinkH);
    }
    var answerEl = wrapper.querySelector(".pending-answer");
    if (answerEl) {
        answerEl.innerHTML = formatContent(pend.content, true);
        answerEl.style.display = pend.content ? "block" : "none";
    }
    // 思考按钮：计时中显示"思考中 Xs"，结束后定格"查看思考过程（Xs）"
    var thinkBtn = wrapper.querySelector(".collapse-btn");
    if (thinkBtn) {
        if (pend.thinkTimer) {
            var secs = Math.floor((Date.now() - pend.thinkStartAt) / 1000);
            thinkBtn.innerHTML = '<span class="arrow">▼</span> 思考中 ' + secs + 's';
        } else if (pend.thinkingSeconds) {
            thinkBtn.innerHTML = '<span class="arrow">▼</span> 查看思考过程（' + pend.thinkingSeconds + 's）';
        }
    }
    // 上下文使用进度改由输入框左侧圆环展示（updateContextRing），不再在消息气泡内渲染
    // 消息区：随内容增长自动滚动，用户上翻历史时保持不动
    stickBottom(area, prevAreaH);
}

function setRing(pct) {
    // 圆环进度：按百分比更新 SVG stroke-dashoffset（r=15.5，周长≈97.4）
    var fg = $("ctxRingWrap") ? $("ctxRingWrap").querySelector(".ctx-ring-fg") : null;
    if (!fg) return;
    var c = 2 * Math.PI * 15.5;
    fg.style.strokeDasharray = c;
    fg.style.strokeDashoffset = c * (1 - pct / 100);
}

function updateContextRing(progress, compacted) {
    // 输入框左侧上下文圆环：环形进度 + hover 显示具体用量（used / window）
    var wrap = $("ctxRingWrap"); if (!wrap) return;
    var tip = $("ctxRingTip"); if (!tip) tip = wrap.querySelector(".ctx-ring-tip");
    if (!progress || !progress.window_tokens) {
        // 无进度数据：圆环置 0，tooltip 显示占位
        setRing(0);
        wrap.title = "上下文使用情况";
        if (tip) tip.textContent = "上下文使用情况";
        return;
    }
    var pct = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    setRing(pct);
    var fg = wrap.querySelector(".ctx-ring-fg");
    if (fg) {
        fg.classList.toggle("warn", pct >= 70 && pct < 90);
        fg.classList.toggle("danger", pct >= 90);
    }
    var base = "上下文使用 " + pct + "%（" + fmtK(progress.used_tokens) + " / " + fmtK(progress.window_tokens) + " tokens）";
    var extra = "";
    if (compacted && compacted.saved_chars) extra = "\n🧹 已压缩，释放 " + fmtNum(compacted.saved_chars) + " 字符";
    wrap.title = base + extra;
    if (tip) {
        tip.innerHTML = "上下文 " + pct + "%<br>" + fmtK(progress.used_tokens) + " / " + fmtK(progress.window_tokens) + " tokens"
            + (compacted && compacted.saved_chars ? "<br><span class=\"tip-compacted\">🧹 已压缩，释放 " + fmtNum(compacted.saved_chars) + " 字符</span>" : "");
    }
}

function removePendingMessage(convId) {
    // 请求完成后移除占位消息，由最终回复消息替换
    var wrapper = document.getElementById("pending-msg-" + convId);
    if (wrapper) wrapper.remove();
}

function fmtNum(n) {
    // token 数字千分位格式化（非数字返回原值）
    var v = Number(n);
    if (isNaN(v)) return String(n == null ? "0" : n);
    return v.toLocaleString("en-US");
}

function fmtK(n) {
    // token 数字压缩为 k 单位：>=1000 显示 x.xk（保留 1 位小数），小于 1000 显示原值
    var v = Number(n);
    if (isNaN(v)) return String(n == null ? "0" : n);
    if (Math.abs(v) >= 1000) return (v / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(Math.round(v));
}

function fmtNow() {
    // 本地当前时间字符串 YYYY-MM-DD HH:MM:SS，用于消息气泡时间显示
    var d = new Date();
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
        + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function fmtMsgTime(ts) {
    // 消息时间显示：兼容 "YYYY-MM-DD HH:MM:SS" / ISO 格式，无则返回空
    if (!ts) return "";
    var s = String(ts).replace("T", " ").replace(/\.\d+Z?$/, "").replace("Z", "");
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) return s.slice(0, 16);
    var d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
        + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function hasChartBlock(content) {
    // 判断回答中是否已包含 ```chart 图表指令块（有则不再显示生成按钮）
    return /```\s*chart/i.test(String(content || ""));
}

function appendMessage(role, content, sql, thinking, evaluator, dialogueId, requestId, thinkingOpen, msgObj) {
    var area = $("chatArea"); if (!area) return;
    var wrapper = document.createElement("div"); wrapper.className = "msg " + role;

    var avatar = document.createElement("div"); avatar.className = "avatar";
    avatar.textContent = role === "user" ? "你" : "AI";

    var bubble = document.createElement("div"); bubble.className = "bubble";
    // 失败/处理中轮无回复时显示占位，避免空气泡（审计信息在数据库中）
    var emptyReply = (role === "assistant" && (!content || !content.trim()));
    bubble.innerHTML = emptyReply
        ? (msgObj && msgObj.status === "processing")
            ? "<p style=\"color:#888\">处理中...（若一直未完成，可能是服务重启导致）</p>"
            : "<p style=\"color:#888\">本轮未返回回复</p>"
        : formatContent(content);

    // 思考过程面板：置于回复内容上方，默认展开，用户折叠/展开状态回写消息记录
    if (thinking && thinking.trim()) {
        var c1 = document.createElement("div"); c1.className = "collapse thinking-panel";
        var btn1 = document.createElement("button"); btn1.className = "collapse-btn";
        var cc1 = document.createElement("div"); cc1.className = "collapse-content"; cc1.textContent = thinking;
        var open1 = thinkingOpen !== false;
        cc1.classList.toggle("show", open1);
        btn1.classList.toggle("open", open1);
        var secs = (msgObj && (msgObj.thinking_seconds != null ? msgObj.thinking_seconds : msgObj.thinkingSeconds)) || 0;
        var thinkLabel = "查看思考过程"
            + (secs ? "（" + secs + "s）" : "");
        btn1.innerHTML = '<span class="arrow">' + (open1 ? "▼" : "▶") + '</span> ' + thinkLabel;
        btn1.onclick = function () {
            var wasOpen = cc1.classList.contains("show"); cc1.classList.toggle("show");
            btn1.classList.toggle("open"); btn1.querySelector(".arrow").textContent = wasOpen ? "▶" : "▼";
            if (msgObj) msgObj.thinkingOpen = !wasOpen;
        };
        c1.appendChild(btn1); c1.appendChild(cc1); bubble.insertBefore(c1, bubble.firstChild);
    }

    if (sql && sql.trim()) {
        var c2 = document.createElement("div"); c2.className = "collapse";
        var btn2 = document.createElement("button"); btn2.className = "collapse-btn";
        btn2.innerHTML = '<span class="arrow">▶</span> 查看执行 SQL';
        var cc2 = document.createElement("div"); cc2.className = "collapse-content"; cc2.textContent = sql;
        btn2.onclick = function () {
            var open = cc2.classList.contains("show"); cc2.classList.toggle("show");
            btn2.classList.toggle("open"); btn2.querySelector(".arrow").textContent = open ? "▶" : "▼";
        };
        c2.appendChild(btn2); c2.appendChild(cc2); bubble.appendChild(c2);
    }

    if (role === "assistant" && requestId) {
        var rid = document.createElement("div");
        rid.className = "request-id";
        rid.textContent = "request_id: " + requestId;
        rid.title = "复制该编号到 trace_view.py 查看本次查询日志";
        bubble.appendChild(rid);
    }

    // token 消耗展示：输入/输出/缓存命中率（历史消息从 msgObj 读取）
    if (role === "assistant" && msgObj && msgObj.llm_tokens) {
        var tu = msgObj.llm_tokens;
        var tEl = document.createElement("div");
        tEl.className = "token-usage";
        // hitRate 用命中/（命中+未命中）计算百分比；无缓存统计时显示占位符
        var _hitTotal = Number(tu.cache_hit || 0) + Number(tu.cache_miss || 0);
        var hitRate = _hitTotal > 0 ? Math.round(Number(tu.cache_hit || 0) * 100 / _hitTotal) + "%" : "--";
        tEl.textContent = "⚡️ 输入总Token：" + fmtK(tu.input_tokens) + " | 输出总Token: " + fmtK(tu.output_tokens)
            + " | 缓存命中率：" + hitRate;
        bubble.appendChild(tEl);
    }

    // 上下文使用进度改由输入框左侧圆环展示（切换会话时由 loadConversation 恢复），不再在消息气泡内渲染

    if (role === "assistant" && evaluator) {
        var scoreArea = document.createElement("div"); scoreArea.className = "score-area";
        scoreArea.innerHTML = "评分: ";
        var did = dialogueId || 0;
        for (var s = 1; s <= 5; s++) {
            (function (idx) {
                var star = document.createElement("button");
                star.className = "star" + (evaluator.user_score && idx <= evaluator.user_score ? " active" : "");
                star.textContent = "★";
                star.onclick = async function () {
                    var stars = scoreArea.querySelectorAll(".star");
                    stars.forEach(function (st, t) { st.classList.toggle("active", t < idx); });
                    try {
                        await fetch(API + "/score", {
                            method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ conversation_id: conversationId, score: idx, dialogue_id: did }),
                        });
                        var done = scoreArea.querySelector(".done"); if (done) done.remove();
                        var d = document.createElement("span"); d.className = "done"; d.textContent = "✓ 感谢反馈";
                        scoreArea.appendChild(d);
                    } catch (e3) {}
                };
                scoreArea.appendChild(star);
            })(s);
        }
        if (evaluator.score) {
            var info = document.createElement("span");
            info.style.cssText = "margin-left:8px;color:#666";
            info.textContent = "(系统自评: " + evaluator.score + "分)";
            scoreArea.appendChild(info);
        }
        bubble.appendChild(scoreArea);
    }

    // 生成图表按钮（仿豆包）：回答未含图表且该轮有可用落盘结果时展示，
    // 点击后由后端按落盘结果自动生成图表，格式与 make_chart 工具完全一致
    // 纯闲聊（chat）/失败（failed）无查询结果，不显示生成按钮
    if (role === "assistant" && requestId && !emptyReply
            && !hasChartBlock(content)
            && (!msgObj || (msgObj.status !== "failed" && msgObj.status !== "chat"))) {
        var genBtn = document.createElement("button");
        genBtn.className = "chart-gen-btn";
        genBtn.type = "button";
        genBtn.textContent = "📊 生成图表";
        genBtn.title = "基于本次查询结果生成图表";
        genBtn.onclick = async function () {
            if (genBtn.disabled) return;
            genBtn.disabled = true;
            genBtn.textContent = "⏳ 图表生成中…";
            try {
                var resp = await fetch(API + "/chart", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        conversation_id: conversationId,
                        request_id: requestId,
                        // auto：由后端按数据形态+查询意图自动挑选图表类型（占比→pie/趋势→line/对比→bar）
                        type: "auto",
                    }),
                });
                var data = await resp.json();
                if (!data.success || !data.charts || !data.charts.length) {
                    genBtn.disabled = false;
                    genBtn.textContent = "📊 生成图表";
                    var errEl = document.createElement("div");
                    errEl.className = "chart-gen-error";
                    errEl.textContent = data.error || "暂无可用数据生成图表";
                    bubble.appendChild(errEl);
                    return;
                }
                // 生成成功：按钮替换为图表，渲染后可点类型切换折线/柱状/饼图
                genBtn.remove();
                data.charts.forEach(function (spec) {
                    var box = document.createElement("div");
                    box.className = "chart-box";
                    // 用 DOM 属性设置原始 JSON：不能再做 &quot; 实体替换，
                    // 否则 dataset 读到的是字面 &quot;，JSON.parse 失败回退成原始 JSON 文本
                    box.setAttribute("data-chart", JSON.stringify(spec));
                    bubble.appendChild(box);
                });
                renderCharts(bubble);
                // 把新图表滚动到视野内（不跳转到最新消息，保持用户所在位置）
                var firstChart = bubble.querySelector(".chart-box");
                if (firstChart) firstChart.scrollIntoView({ behavior: "smooth", block: "nearest" });
            } catch (e) {
                genBtn.disabled = false;
                genBtn.textContent = "📊 生成图表";
            }
        };
        bubble.appendChild(genBtn);
    }

    // 消息发送时间：从消息记录读取 request_at（随 MySQL 落盘），无则省略
    if (msgObj && msgObj.request_at) {
        var tEl = document.createElement("div");
        tEl.className = "msg-time";
        tEl.textContent = fmtMsgTime(msgObj.request_at);
        bubble.appendChild(tEl);
    }

    wrapper.appendChild(avatar); wrapper.appendChild(bubble);
    area.appendChild(wrapper); scrollToBottom(area, true);
    renderCharts(bubble);
}

function formatContent(text, streaming) {
    // 极简 Markdown 渲染（安全：先转义 HTML 防注入，再按块解析）
    // 支持：代码块 / 标题 / 引用 / 列表 / 表格 / 段落 + 行内粗体斜体代码
    if (!text) return "";
    var html = "";
    var lines = String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").split(/\r?\n/);
    var i = 0, inList = false, inTable = false, para = [], listTag = "ul";
    function mdInline(s) {
        s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
        s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
        s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
        s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
        return s;
    }
    function flushPara() { if (para.length) { html += "<p>" + para.map(mdInline).join("<br>") + "</p>"; para = []; } }
    function flushList() { if (inList) { html += "</" + listTag + ">"; inList = false; } }
    function flushTable() { if (inTable) { html += "</table>"; inTable = false; } }
    while (i < lines.length) {
        var line = lines[i];
        if (/^```/.test(line)) { // 代码块（含 ```chart 图表指令块）
            flushPara(); flushList(); flushTable();
            var lang = line.replace(/^```\s*/, "").trim(); // 提取语言标记，如 chart
            var buf = []; i++;
            while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
            i++;
            if (lang === "chart") {
                // 图表指令块：数据必须来自 execute_query 真实结果，流式中先显示占位，完成后由 renderCharts 渲染
                var chartRaw = buf.join("\n");
                if (streaming) {
                    html += '<div class="chart-box chart-pending"><span>📊 图表生成中…</span></div>';
                } else {
                    var chartJson = null;
                    try { chartJson = JSON.stringify(JSON.parse(chartRaw.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">"))); } catch (e2) { chartJson = null; }
                    if (chartJson) {
                        html += '<div class="chart-box" data-chart="' + chartJson.replace(/"/g, "&quot;") + '"></div>';
                    } else {
                        html += "<pre><code>" + buf.join("\n") + "</code></pre>";
                    }
                }
                continue;
            }
            html += "<pre><code>" + buf.join("\n") + "</code></pre>";
            continue;
        }
        var hm = line.match(/^(#{1,4})\s+(.*)$/); // 标题
        if (hm) {
            flushPara(); flushList(); flushTable();
            var lvl = hm[1].length;
            html += "<h" + lvl + ">" + mdInline(hm[2]) + "</h" + lvl + ">";
            i++; continue;
        }
        if (/^&gt;\s?/.test(line)) { // 引用
            flushPara(); flushList(); flushTable();
            html += "<blockquote>" + mdInline(line.replace(/^&gt;\s?/, "")) + "</blockquote>";
            i++; continue;
        }
        var lm = line.match(/^[-*]\s+(.*)$/) || line.match(/^\d+\.\s+(.*)$/); // 列表
        if (lm) {
            flushPara(); flushTable();
            // 数字开头渲染为有序列表 <ol>，-/* 渲染为无序列表 <ul>
            var nextTag = /^\d+\.\s+/.test(line) ? "ol" : "ul";
            if (!inList || listTag !== nextTag) {
                if (inList) { html += "</" + listTag + ">"; }
                html += "<" + nextTag + ">";
                listTag = nextTag;
                inList = true;
            }
            html += "<li>" + mdInline(lm[1]) + "</li>";
            i++; continue;
        }
        if (/^\|/.test(line) && /\|$/.test(line)) { // 表格
            var cells = line.split("|").slice(1, -1).map(function (c) { return c.trim(); });
            var isSep = cells.every(function (c) { return /^:?-+:?$/.test(c); });
            if (!inTable && !isSep) {
                flushPara(); flushList();
                html += "<table><thead><tr>" + cells.map(function (c) { return "<th>" + mdInline(c) + "</th>"; }).join("") + "</tr></thead><tbody>";
                inTable = true;
            } else if (inTable && !isSep) {
                html += "<tr>" + cells.map(function (c) { return "<td>" + mdInline(c) + "</td>"; }).join("") + "</tr>";
            }
            i++; continue;
        }
        if (/^\s*$/.test(line)) { flushPara(); flushList(); flushTable(); i++; continue; }
        flushList(); flushTable();
        para.push(line);
        i++;
    }
    flushPara(); flushList(); flushTable();
    return html;
}

function chartResizeAll() {
    // 容器尺寸变化（窗口缩放/侧栏展开收起）后让已渲染图表重新适配，避免 canvas 遮挡或裁切
    document.querySelectorAll(".chart-box[data-chart]").forEach(function (el) {
        if (el._chart && typeof el._chart.resize === "function") {
            try { el._chart.resize(); } catch (e) {}
        }
    });
}

function renderCharts(root) {
    // 扫描并渲染图表块：数据来自 execute_query 真实结果，仅渲染一次，避免流式反复重建
    if (!root || typeof window.echarts === "undefined") {
        // ECharts 未加载时回退展示原始 JSON，保证内容可见
        if (root) root.querySelectorAll(".chart-box[data-chart]").forEach(function (el) {
            var txt = el.dataset.chart || "";
            try { txt = JSON.stringify(JSON.parse(txt), null, 2); } catch (e) {}
            el.innerHTML = "<pre style=\"margin:0;white-space:pre-wrap;font-size:12px;color:#ACACBE\">" + txt.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;") + "</pre>";
        });
        return;
    }
    root.querySelectorAll(".chart-box[data-chart]").forEach(function (el) {
        if (el._chartRendered) return;
        try {
            var spec = JSON.parse(el.dataset.chart || "null");
            if (!spec || !spec.type) throw new Error("bad chart spec");
            var opt = buildChartOption(spec);
            // 数据为空兜底：不渲染空白图，回退展示原始 JSON，避免"看不到图表"
            var hasData = (opt.series || []).some(function (s) { return s.data && s.data.length; });
            if (!hasData) throw new Error("empty chart data");
            var chart = echarts.init(el);
            chart.setOption(opt);
            el._chart = chart;
            el._chartSpec = spec;
            el._chartRendered = true;
        } catch (e) {
            // 解析/渲染失败或数据为空：回退展示原始 JSON，不显示空白图表框
            el.innerHTML = "<pre style=\"margin:0;white-space:pre-wrap;font-size:12px;color:#ACACBE\">" + (el.dataset.chart || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;") + "</pre>";
        }
    });
}

function normalizeChartData(spec) {
    // 统一各输入格式为 { categories, seriesList:[{name,values}], xName, yName }
    // 支持：xAxis(数组)+series / xField+yField / xField+yFields / pie(nameField,valueField)
    // 兼容 xAxis/yAxis 为对象（{field,name}）或字符串 的写法，避免 LLM 输出差异导致空白图
    var xField = spec.xField;
    var yField = spec.yField;
    var xName = spec.xName || spec.xAxisName || "";
    var yName = spec.yName || spec.yAxisName || "";
    if (spec.xAxis && typeof spec.xAxis === "object" && !Array.isArray(spec.xAxis)) {
        xField = spec.xAxis.field || xField;
        xName = spec.xAxis.name || xName;
    } else if (typeof spec.xAxis === "string") {
        xField = spec.xAxis;
    }
    if (spec.yAxis && typeof spec.yAxis === "object" && !Array.isArray(spec.yAxis)) {
        yField = spec.yAxis.field || yField;
        yName = spec.yAxis.name || yName;
    } else if (typeof spec.yAxis === "string") {
        yField = spec.yAxis;
    }
    var categories = Array.isArray(spec.xAxis) ? spec.xAxis.slice() : [];
    var seriesList = (spec.series || []).map(function (s) {
        return { name: s.name || "", values: s.data || [] };
    });
    if (!seriesList.length && Array.isArray(spec.data) && spec.data.length) {
        // 多序列：yField/yFields 均可能为数组（兼容 LLM 两种写法）
        var yFieldList = Array.isArray(spec.yField) ? spec.yField : (Array.isArray(spec.yFields) ? spec.yFields : null);
        if (xField && yFieldList) {
            categories = spec.data.map(function (d) { return d[xField]; });
            seriesList = yFieldList.map(function (f) { return { name: f, values: spec.data.map(function (d) { return d[f]; }) }; });
        } else if (xField && yField) {
            categories = spec.data.map(function (d) { return d[xField]; });
            seriesList = [{ name: spec.seriesName || yName || yField, values: spec.data.map(function (d) { return d[yField]; }) }];
        } else if (spec.nameField && spec.valueField) {
            // 扁平饼图：nameField/valueField
            categories = spec.data.map(function (d) { return d[spec.nameField]; });
            seriesList = [{ name: spec.seriesName || spec.name || "占比", values: spec.data.map(function (d) { return d[spec.valueField]; }) }];
        }
    }
    // series 数据为 {name,value} 对象数组（标准饼图）时归一化为 categories + values
    if (!categories.length && seriesList.length === 1 && seriesList[0].values.length && typeof seriesList[0].values[0] === "object") {
        categories = seriesList[0].values.map(function (v) { return v.name; });
        seriesList[0].values = seriesList[0].values.map(function (v) { return v.value; });
    }
    return {
        categories: categories,
        seriesList: seriesList,
        xName: xName || xField || "",
        yName: yName || yField || "",
    };
}

function buildPieData(categories, values) {
    // 饼图数据预处理：数值化并过滤非正值；扇区过多时只保留最大的前 N-1 个，
    // 其余合并为“其他”，避免几十个扇区导致标签相互重叠（沿用业界常见做法）
    var items = [];
    categories.forEach(function (c, i) {
        var v = Number(values[i]);
        if (!isNaN(v) && v > 0) items.push({ name: c, value: v });
    });
    items.sort(function (a, b) { return b.value - a.value; });
    var MAX_SLICES = 8;
    if (items.length > MAX_SLICES) {
        var keep = items.slice(0, MAX_SLICES - 1);
        var rest = items.slice(MAX_SLICES - 1);
        var restSum = rest.reduce(function (acc, x) { return acc + x.value; }, 0);
        keep.push({ name: "其他", value: restSum });
        items = keep;
    }
    return items;
}

function buildChartOption(spec, type) {
    // 把 LLM 输出的简单图表描述转成 ECharts option（深色主题适配当前界面）
    // type 缺省时用 spec.type（图表类型已由 AI 决定，不再提供用户切换）
    var dark = { text: "#ECECF1", sub: "#9A9AAC", line: "#7A7A8A", split: "#3A3A44" };
    type = type || spec.type || "bar";
    var norm = normalizeChartData(spec);
    var categories = norm.categories;
    var seriesList = norm.seriesList;
    if (!seriesList.length) seriesList = [{ name: "", values: [] }];
    var opt = {
        backgroundColor: "transparent",
        color: ["#10A37F", "#4E8CF0", "#F0C040", "#E46C6C", "#8A6CF0", "#3EC6E0"],
        title: spec.title ? { text: spec.title, left: "center", top: 4, textStyle: { color: dark.text, fontSize: 14 } } : undefined,
        tooltip: { trigger: type === "pie" ? "item" : "axis" },
        textStyle: { color: dark.text },
        grid: { left: norm.yName ? 64 : 48, right: 24, top: spec.title ? 44 : 24, bottom: norm.xName ? 44 : 36, containLabel: true },
    };
    if (type === "pie") {
        opt.series = seriesList.map(function (s) {
            return {
                name: s.name, type: "pie", radius: ["0%", "52%"], center: ["36%", "50%"],
                data: buildPieData(categories, s.values),
                minAngle: 2,
                avoidLabelOverlap: true,
                label: { color: dark.text, fontSize: 11, formatter: "{b}: {d}%", overflow: "truncate", width: 70 },
                labelLine: { length: 10, length2: 8, lineStyle: { color: dark.sub } },
                itemStyle: { borderColor: "#212121", borderWidth: 1 }
            };
        });
        // 图例放右侧避免遮挡饼图底部与标签，扇区多时可滚动；给图例固定宽度，避免窄屏下遮挡饼图
        opt.legend = { orient: "vertical", right: 4, top: "middle", width: 86, type: "scroll", itemWidth: 9, itemHeight: 9, itemGap: 3, textStyle: { color: dark.sub, fontSize: 11 } };
    } else {
        opt.xAxis = { type: "category", data: categories, name: norm.xName, nameTextStyle: { color: dark.sub }, axisLine: { lineStyle: { color: dark.line, width: 1.5 } }, axisLabel: { color: dark.text } };
        opt.yAxis = { type: "value", name: norm.yName, nameTextStyle: { color: dark.sub }, axisLine: { lineStyle: { color: dark.line, width: 1.5 } }, splitLine: { lineStyle: { color: dark.split } }, axisLabel: { color: dark.sub } };
        opt.series = seriesList.map(function (s) {
            var item = { name: s.name, type: type === "area" ? "line" : type, data: s.values };
            if (type === "line" || type === "area") { item.smooth = true; item.lineStyle = { width: 2 }; }
            if (type === "area") { item.areaStyle = {}; }
            if (type === "bar") { item.barMaxWidth = 40; }
            return item;
        });
        // 底部可拖拽滑块调整横轴显示范围（常见于按日期查看区间），同时支持滚轮/拖拽缩放
        opt.grid.bottom = 58;
        opt.dataZoom = [
            { type: "slider", xAxisIndex: 0, height: 16, bottom: 6, showDataShadow: false,
              borderColor: dark.line, textStyle: { color: dark.sub, fontSize: 10 },
              fillerColor: "rgba(16,163,127,0.16)", handleStyle: { color: dark.text } },
            { type: "inside", xAxisIndex: 0, zoomOnMouseWheel: true, moveOnMouseMove: true },
            { type: "inside", yAxisIndex: 0, zoomOnMouseWheel: true }
        ];
        // 0 参考线：数据存在负值时在 y=0 画一条醒目的横线，便于观测正负
        var hasNegative = seriesList.some(function (s) {
            return (s.values || []).some(function (v) { return typeof v === "number" && v < 0; });
        });
        if (hasNegative) {
            opt.series.forEach(function (item) {
                item.markLine = { silent: true, symbol: "none", lineStyle: { color: "#F0C040", width: 1.2 }, label: { show: false }, data: [{ yAxis: 0 }] };
            });
        }
        opt.legend = { top: 0, right: 8, textStyle: { color: dark.sub } };
    }
    return opt;
}



function openNameOverlay() {
    // 手动重新设置称呼：预填当前称呼（匿名的留空），显示弹层并聚焦
    _nameOverlayManual = true;
    var overlay = $("nameOverlay");
    if (!overlay) return;
    var input = $("nameInput");
    if (input) {
        input.value = (creator && creator.indexOf("匿名-") !== 0) ? creator : "";
        input.focus();
    }
    overlay.style.display = "flex";
    // 点击遮罩等同“取消”，关闭不改
    overlay.onclick = function (e) { if (e.target === overlay) closeNameOverlay(); };
}

function closeNameOverlay() {
    _nameOverlayManual = false;
    var overlay = $("nameOverlay");
    if (overlay) overlay.style.display = "none";
}

function _applyCreator(value, fromBootstrap) {
    // 设置创建人并持久化（命名或跳过共用）；不自动新建会话，由用户点击“新建对话”
    // 首次进入弹层（fromBootstrap=true）受 _bootstrapDone 防重约束；手动“设置称呼”随时可改
    if (fromBootstrap) {
        if (_bootstrapDone) return;
        _bootstrapDone = true;
    }
    creator = value;
    localStorage.setItem("creator", creator);
    var overlay = $("nameOverlay");
    if (overlay) overlay.style.display = "none";
    refreshConvList();
}

function confirmName() {
    // 用户点击“确定”：有输入则用输入值；手动设置时留空=关闭不改，首次留空=分配匿名标识
    var input = $("nameInput");
    var name = (input && input.value) ? input.value.trim() : "";
    if (_nameOverlayManual) {
        if (!name) { closeNameOverlay(); return; }
        _applyCreator(name, false);
    } else {
        _applyCreator(name || ("匿名-" + Math.random().toString(16).slice(2, 6)), true);
    }
}

function skipName() {
    // 用户点击“取消/跳过”：手动弹层=关闭不改；首次进入=分配匿名标识
    if (_nameOverlayManual) { closeNameOverlay(); return; }
    _applyCreator("匿名-" + Math.random().toString(16).slice(2, 6), true);
}

(function init() {
    // 图表随窗口/侧栏尺寸变化自适应，避免容器大小改变后出现遮挡
    window.addEventListener("resize", function () { chartResizeAll(); });
    // 准实时刷新左侧会话状态：处理中显示加载圈，完成后变绿色待点击
    setInterval(pollConvActivity, 2500);
    pollConvActivity();
    // 创建人：首次进入在页面内弹层让用户自行选择“确定”或“跳过”（不用 prompt，切窗口不消失）
    creator = localStorage.getItem("creator") || "";
    // 旧逻辑残留的“未命名”也视为未设置，让用户重新选择一次
    // 打开页面先初始化上下文圆环为 0（避免空白页 SVG 未设 dashoffset 显示成满环）
    updateContextRing(null, null);
    if (creator && creator !== "未命名") {
        // 打开页面只加载会话列表（由用户点“新建对话”或已有会话），
        // 刷新后恢复用户原本查看的会话（若仍存在）
        refreshConvList().then(function () {
            var saved = localStorage.getItem("currentConvId");
            if (saved && conversations[saved]) loadConversation(saved);
        });
    } else {
        var overlay = $("nameOverlay");
        if (overlay) {
            overlay.style.display = "flex";
            // 点击遮罩视为“跳过”（匿名关闭），_bootstrapDone 保证只创建一次
            overlay.onclick = function (e) { if (e.target === overlay) skipName(); };
        }
        var input = $("nameInput");
        if (input) input.focus();
    }
})();
