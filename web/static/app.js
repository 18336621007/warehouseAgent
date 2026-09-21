// 智能数仓助手 - 前端（流式 + 打分 + 重命名/删除）
var API = "/api";
// 当前选中的完整前端对话标识
var conversationId = null;
var conversations = {};
// 创建人（仅用于区分不同用户的对话，测试用），localStorage 持久化
var creator = localStorage.getItem("creator") || "";
// 初始会话引导只执行一次：防止命名弹层被重复触发（连点/双击/回车+点击）创建多个会话
var _bootstrapDone = false;
// 每个会话的进行中请求状态（conversationId -> {thinking,status,content,...}），
// 切换会话后占位消息与会话绑定，不共享同一个进度条
var pendingRequests = {};

function $(id) { return document.getElementById(id); }
function hideEmpty() { var el = $("emptyState"); if (el) el.style.display = "none"; }

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
        conversations[conversationId] = { title: "新对话", creator: creator, messages: [], _loadedFromServer: true };
        $("chatArea").innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
        refreshConvList();
        updateInputLock();
        updateContextRing(null, null);
        return true;
    } catch (e) { return false; }
}

async function loadConversation(conversationIdToLoad) {
    conversationId = conversationIdToLoad;
    var conv = conversations[conversationIdToLoad];
    // 内存缓存为空（刷新/重启后）时从服务端拉取完整对话（含他人创建的历史）
    if (!conv || !conv.messages || conv.messages.length === 0) {
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
        conv = conversations[conversationIdToLoad];
    }
    var area = $("chatArea");
    area.innerHTML = "";
    if (conv && conv.messages && conv.messages.length > 0) {
        conv.messages.forEach(function (m) { appendMessage(m.role, m.content, m.sql, m.thinking, m.evaluator, m.dialogue_id, m.request_id, m.thinkingOpen, m); });
    } else {
        area.innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
    }
    // 该会话仍有进行中请求时恢复占位消息（思考内容从状态对象读取）
    if (pendingRequests[conversationIdToLoad]) appendPendingMessage(conversationIdToLoad);
    refreshConvList();
    updateInputLock();
    // 上下文圆环用服务端从 Checkpoint 实时估算的占用恢复（不同会话各自独立，不依赖落盘快照）
    updateContextRing(conv && conv._context, null);
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
            $("chatArea").innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
        }
        refreshConvList();
        updateInputLock();
    } catch (e) {}
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
    pendingRequests[reqConv] = {
        thinking: "", status: "AI 正在思考...",
        content: "", sql: "", evaluator: null, dialogue_id: 0, request_id: "",
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
    lockInput(true);
    hideEmpty();
    appendMessage("user", msg);
    if (conv) conv.messages.push({ role: "user", content: msg });
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
                    } else if (event.type === "error") {
                        pend.doneReceived = true;
                        if (pend.thinkTimer) stopThinkTimer(pend);
                        flushAnswerTypewriter(pend);
                        var errorIdText = event.error_id
                            ? "\n错误编号：" + event.error_id
                            : "";
                        pend.content = event.text + errorIdText;
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
    };
    if (conv) conv.messages.push(savedMsg);
    delete pendingRequests[convId];
    // 只有当前仍显示发起请求的会话时才更新 DOM 与焦点
    if (conversationId === convId) {
        removePendingMessage(convId);
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
    area.appendChild(wrapper); area.scrollTop = area.scrollHeight;
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
    var statusEl = wrapper.querySelector(".pending-status");
    if (statusEl) statusEl.textContent = pend.status;
    var thinkEl = wrapper.querySelector(".pending-thinking");
    if (thinkEl) {
        thinkEl.textContent = pend.thinking;
        // 思考面板：用户未主动上翻时自动滚动到底部，保证始终看到最新内容
        if (!thinkEl._userScrolledUp) thinkEl.scrollTop = thinkEl.scrollHeight;
    }
    var answerEl = wrapper.querySelector(".pending-answer");
    if (answerEl) {
        answerEl.innerHTML = formatContent(pend.content);
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
    if (area && !area._userScrolledUp) area.scrollTop = area.scrollHeight;
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
    var base = "上下文使用 " + pct + "%（" + fmtNum(progress.used_tokens) + " / " + fmtNum(progress.window_tokens) + " tokens）";
    var extra = "";
    if (compacted && compacted.saved_chars) extra = "\n🧹 已压缩，释放 " + fmtNum(compacted.saved_chars) + " 字符";
    wrap.title = base + extra;
    if (tip) {
        tip.innerHTML = "上下文 " + pct + "%<br>" + fmtNum(progress.used_tokens) + " / " + fmtNum(progress.window_tokens) + " tokens"
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

function appendMessage(role, content, sql, thinking, evaluator, dialogueId, requestId, thinkingOpen, msgObj) {
    var area = $("chatArea"); if (!area) return;
    var wrapper = document.createElement("div"); wrapper.className = "msg " + role;

    var avatar = document.createElement("div"); avatar.className = "avatar";
    avatar.textContent = role === "user" ? "你" : "AI";

    var bubble = document.createElement("div"); bubble.className = "bubble";
    // 失败轮无回复时显示占位，避免空气泡（审计信息在数据库中）
    bubble.innerHTML = (role === "assistant" && (!content || !content.trim()))
        ? "<p style=\"color:#888\">本轮未返回回复</p>"
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

    // token 消耗展示：输入/输出/缓存命中/未命中（历史消息从 msgObj 读取）
    if (role === "assistant" && msgObj && msgObj.llm_tokens) {
        var tu = msgObj.llm_tokens;
        var tEl = document.createElement("div");
        tEl.className = "token-usage";
        tEl.textContent = "⚡️ 输入总Token：" + fmtNum(tu.input_tokens) + " | 输出总Token: " + fmtNum(tu.output_tokens)
            + " | 缓存命中：" + fmtNum(tu.cache_hit) + " | 缓存未命中：" + fmtNum(tu.cache_miss);
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

    wrapper.appendChild(avatar); wrapper.appendChild(bubble);
    area.appendChild(wrapper); area.scrollTop = area.scrollHeight;
}

function formatContent(text) {
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
        if (/^```/.test(line)) { // 代码块
            flushPara(); flushList(); flushTable();
            var buf = []; i++;
            while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
            i++;
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


function _applyCreator(value) {
    // 设置创建人并持久化（命名或跳过共用）；不自动新建会话，由用户点击“新建对话”
    // 已触发过则忽略，防止重复创建会话
    if (_bootstrapDone) return;
    _bootstrapDone = true;
    creator = value;
    localStorage.setItem("creator", creator);
    var overlay = $("nameOverlay");
    if (overlay) overlay.style.display = "none";
    refreshConvList();
}

function confirmName() {
    // 用户点击“确定”：有输入则用输入值，留空则分配匿名标识
    var input = $("nameInput");
    var name = (input && input.value) ? input.value.trim() : "";
    _applyCreator(name || ("匿名-" + Math.random().toString(16).slice(2, 6)));
}

function skipName() {
    // 用户点击“跳过”：直接分配匿名标识，不打扰
    _applyCreator("匿名-" + Math.random().toString(16).slice(2, 6));
}

(function init() {
    // 创建人：首次进入在页面内弹层让用户自行选择“确定”或“跳过”（不用 prompt，切窗口不消失）
    creator = localStorage.getItem("creator") || "";
    // 旧逻辑残留的“未命名”也视为未设置，让用户重新选择一次
    // 打开页面先初始化上下文圆环为 0（避免空白页 SVG 未设 dashoffset 显示成满环）
    updateContextRing(null, null);
    if (creator && creator !== "未命名") {
        // 打开页面只加载会话列表，不自动新建会话（由用户点“新建对话”或已有会话）
        refreshConvList();
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
