// 智能数仓助手 - 前端（流式 + 打分 + 重命名/删除）
var API = "/api";
// 当前选中的完整前端对话标识
var conversationId = null;
var conversations = {};
// 每个会话的进行中请求状态（conversationId -> {thinking,status,content,...}），
// 切换会话后占位消息与会话绑定，不共享同一个进度条
var pendingRequests = {};

function $(id) { return document.getElementById(id); }
function hideEmpty() { var el = $("emptyState"); if (el) el.style.display = "none"; }

async function newChat() {
    try {
        var res = await fetch(API + "/conversations", { method: "POST" });
        var data = await res.json();
        conversationId = data.conversation_id;
        conversations[conversationId] = { title: "新对话", messages: [] };
        $("chatArea").innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
        refreshConvList();
        updateInputLock();
        return true;
    } catch (e) { return false; }
}

function loadConversation(conversationIdToLoad) {
    conversationId = conversationIdToLoad;
    var conv = conversations[conversationIdToLoad];
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
            conversations[c.conversation_id] = conversations[c.conversation_id] || { title: c.title, messages: [] };
            var div = document.createElement("div");
            div.className = "conv-item" + (c.conversation_id === conversationId ? " active" : "");
            div.onclick = function () { loadConversation(c.conversation_id); };

            var span = document.createElement("span");
            span.textContent = c.title || "新对话";
            span.style.flex = "1";
            span.style.overflow = "hidden";
            span.style.textOverflow = "ellipsis";
            div.appendChild(span);

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
                    } else if (event.type === "done") {
                        pend.doneReceived = true;
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
                    } else if (event.type === "error") {
                        pend.doneReceived = true;
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

    // 消息区滚动监听只绑定一次：用户上翻历史时暂停自动滚动
    if (!area._scrollBound) {
        area._scrollBound = true;
        area.addEventListener("scroll", function () {
            area._userScrolledUp = area.scrollTop + area.clientHeight < area.scrollHeight - 40;
        });
    }
    wrapper.appendChild(avatar); wrapper.appendChild(bubble);
    area.appendChild(wrapper); area.scrollTop = area.scrollHeight;
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
        answerEl.textContent = pend.content;
        answerEl.style.display = pend.content ? "block" : "none";
    }
    // 消息区：随内容增长自动滚动，用户上翻历史时保持不动
    if (area && !area._userScrolledUp) area.scrollTop = area.scrollHeight;
}

function removePendingMessage(convId) {
    // 请求完成后移除占位消息，由最终回复消息替换
    var wrapper = document.getElementById("pending-msg-" + convId);
    if (wrapper) wrapper.remove();
}

function appendMessage(role, content, sql, thinking, evaluator, dialogueId, requestId, thinkingOpen, msgObj) {
    var area = $("chatArea"); if (!area) return;
    var wrapper = document.createElement("div"); wrapper.className = "msg " + role;

    var avatar = document.createElement("div"); avatar.className = "avatar";
    avatar.textContent = role === "user" ? "你" : "AI";

    var bubble = document.createElement("div"); bubble.className = "bubble";
    bubble.innerHTML = formatContent(content);

    // 思考过程面板：置于回复内容上方，默认展开，用户折叠/展开状态回写消息记录
    if (thinking && thinking.trim()) {
        var c1 = document.createElement("div"); c1.className = "collapse thinking-panel";
        var btn1 = document.createElement("button"); btn1.className = "collapse-btn";
        var cc1 = document.createElement("div"); cc1.className = "collapse-content"; cc1.textContent = thinking;
        var open1 = thinkingOpen !== false;
        cc1.classList.toggle("show", open1);
        btn1.classList.toggle("open", open1);
        btn1.innerHTML = '<span class="arrow">' + (open1 ? "▼" : "▶") + '</span> 查看思考过程';
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
    if (!text) return "";
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/```(\w*)\n?([\s\S]*?)```/g, "<pre>$2</pre>")
        .replace(/\n/g, "<br>");
}

(function init() {
    newChat().then(function (ok) {
        if (!ok) { var el = $("emptyState"); if (el) el.textContent = "无法连接服务器，请启动: python web/server.py"; }
    });
})();
