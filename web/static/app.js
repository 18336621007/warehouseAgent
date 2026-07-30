// 智能数仓助手 - 前端（流式 + 打分 + 重命名/删除）
var API = "/api";
// 当前选中的完整前端对话标识
var conversationId = null;
var conversations = {};

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
        return true;
    } catch (e) { return false; }
}

function loadConversation(conversationIdToLoad) {
    conversationId = conversationIdToLoad;
    var conv = conversations[conversationIdToLoad];
    var area = $("chatArea");
    area.innerHTML = "";
    if (conv && conv.messages && conv.messages.length > 0) {
        conv.messages.forEach(function (m) { appendMessage(m.role, m.content, m.sql, m.thinking, m.evaluator, m.dialogue_id); });
    } else {
        area.innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
    }
    refreshConvList();
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
        delete conversations[conversationIdToDelete];
        if (conversationId === conversationIdToDelete) {
            conversationId = null;
            $("chatArea").innerHTML = '<div class="empty-state" id="emptyState">新建对话，开始查询吧</div>';
        }
        refreshConvList();
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

async function sendMsg() {
    var input = $("msgInput"); if (!input) return;
    var msg = input.value.trim(); if (!msg) return;

    if (!conversationId) {
        lockInput(true);
        var ok = await newChat();
        if (!ok) { appendMessage("assistant", "无法连接服务器，请确认已启动: python web/server.py"); lockInput(false); return; }
    }

    var conv = conversations[conversationId];
    if (conv && (!conv.title || conv.title === "新对话")) conv.title = msg.slice(0, 40);

    input.value = "";
    lockInput(true);
    hideEmpty();
    appendMessage("user", msg);
    if (conv) conv.messages.push({ role: "user", content: msg });

    var loadingId = showLoading();
        var thinkingText = "";
    console.log("[sendMsg] user=" + msg.slice(0, 60) + " conversation=" + conversationId);
    var finalContent = "", finalSql = "", finalEval = null, finalDialogueId = 0;

    try {
        var res = await fetch(API + "/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ conversation_id: conversationId, message: msg }),
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
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i];
                if (!line.startsWith("data: ")) continue;
                try {
                    var event = JSON.parse(line.slice(6));
                    if (event.type === "status" || event.type === "thinking") {
                        updateLoading(loadingId, event.text);
                        thinkingText += event.text + "\n";
                    } else if (event.type === "done") {
                                                finalContent = event.content;
                        console.log("[sendMsg] done: content=" + (finalContent || "").slice(0, 80));
                        finalSql = event.sql;
                        finalEval = event.evaluator;
                        finalDialogueId = event.dialogue_id || 0;
                    } else if (event.type === "error") {
                                                finalContent = "处理出错: " + event.text;
                        console.log("[sendMsg] error: " + event.text);
                    }
                } catch (parseErr) {}
            }
        }
    } catch (e) {
        finalContent = "连接失败，请确认服务已启动。";
    }

    removeLoading(loadingId);
        console.log("[sendMsg] finalContent=" + (finalContent || "(empty)").slice(0, 100));
    appendMessage("assistant", finalContent || "(无响应)", finalSql, thinkingText, finalEval, finalDialogueId);
    if (conv) {
        conv.messages.push({
            role: "assistant", content: finalContent, sql: finalSql,
            thinking: thinkingText, evaluator: finalEval, dialogue_id: finalDialogueId,
        });
    }
    refreshConvList();
    lockInput(false);
    input.focus();
}

function updateLoading(id, text) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = '<span class="spinner"></span> ' + text;
}

function appendMessage(role, content, sql, thinking, evaluator, dialogueId) {
    var area = $("chatArea"); if (!area) return;
    var wrapper = document.createElement("div"); wrapper.className = "msg " + role;

    var avatar = document.createElement("div"); avatar.className = "avatar";
    avatar.textContent = role === "user" ? "你" : "AI";

    var bubble = document.createElement("div"); bubble.className = "bubble";
    bubble.innerHTML = formatContent(content);

    if (thinking && thinking.trim()) {
        var c1 = document.createElement("div"); c1.className = "collapse";
        var btn1 = document.createElement("button"); btn1.className = "collapse-btn";
        btn1.innerHTML = '<span class="arrow">▶</span> 查看思考过程';
        var cc1 = document.createElement("div"); cc1.className = "collapse-content"; cc1.textContent = thinking;
        btn1.onclick = function () {
            var open = cc1.classList.contains("show"); cc1.classList.toggle("show");
            btn1.classList.toggle("open"); btn1.querySelector(".arrow").textContent = open ? "▶" : "▼";
        };
        c1.appendChild(btn1); c1.appendChild(cc1); bubble.appendChild(c1);
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

    if (role === "assistant" && evaluator) {
        var scoreArea = document.createElement("div"); scoreArea.className = "score-area";
        scoreArea.innerHTML = "评分: ";
        var did = dialogueId || 0;
        for (var i = 1; i <= 5; i++) {
            (function (idx) {
                var star = document.createElement("button");
                star.className = "star" + (evaluator.user_score && idx <= evaluator.user_score ? " active" : "");
                star.textContent = "★";
                star.onclick = async function () {
                    var stars = scoreArea.querySelectorAll(".star");
                    stars.forEach(function (s, j) { s.classList.toggle("active", j < idx); });
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
            })(i);
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

function showLoading() {
    var area = $("chatArea"); if (!area) return "";
    var div = document.createElement("div"); div.className = "loading";
    div.id = "load-" + Date.now(); div.innerHTML = '<span class="spinner"></span> AI 正在思考...';
    area.appendChild(div); area.scrollTop = area.scrollHeight;
    return div.id;
}

function removeLoading(id) { var el = document.getElementById(id); if (el) el.remove(); }

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
