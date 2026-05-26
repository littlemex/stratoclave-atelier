// stratoclave-atelier chat (Stage G): vanilla ES module, no build step.
//
// Drives the single-page chat at "/" against the Stage G surface:
//   POST /api/sessions                          -> create root session
//   POST /api/sessions/{id}/agent-runs          -> kick off an agent run
//   GET  /api/sessions/{id}/events?follow=true  -> SSE stream
//   POST /api/sessions/{id}/freeze              -> freeze whole session
//
// State is intentionally a flat module-level object: this is a thin
// driver, not a framework. The four-panel UI from Stages B-F lives at
// /panels and is unaffected.

const state = {
    sessionId: null,
    eventSource: null,
    /**
     * Map of agent_run sequence -> {<li>, <pre>} for streaming deltas.
     * Stage G emits one user-turn event followed by N agent_chunk
     * (text_delta) events and one agent_turn end-of-turn summary; we
     * collapse all chunks following the latest user turn into one
     * assistant bubble keyed on the user-turn seq.
     */
    streamingAssistantEl: null,
    streamingAssistantText: null,
    submitting: false,
};

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async function api(method, path, body) {
    const init = { method, headers: {} };
    if (body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
    }
    const resp = await fetch(path, init);
    const text = await resp.text();
    if (!resp.ok) {
        throw new Error(`${method} ${path} -> ${resp.status} ${text}`);
    }
    return text ? JSON.parse(text) : null;
}

function flash(message) {
    const bar = document.getElementById("status-bar");
    bar.textContent = message;
    bar.classList.add("show");
    setTimeout(() => bar.classList.remove("show"), 1800);
}

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

function chatLog() {
    return document.getElementById("chat-log");
}

function emptyHint() {
    return document.getElementById("chat-empty");
}

function hideEmptyHint() {
    const hint = emptyHint();
    if (hint && !hint.hasAttribute("hidden")) {
        hint.setAttribute("hidden", "");
    }
}

function appendMessage({ role, content, streaming = false, badge = null }) {
    hideEmptyHint();
    const li = document.createElement("li");
    li.className = `chat-message role-${role}`;
    li.dataset.role = role;

    const meta = document.createElement("div");
    meta.className = "chat-meta";
    const roleSpan = document.createElement("span");
    roleSpan.className = "role";
    roleSpan.textContent = role;
    meta.appendChild(roleSpan);
    if (badge) {
        const badgeSpan = document.createElement("span");
        badgeSpan.className = "badge";
        badgeSpan.textContent = badge;
        meta.appendChild(badgeSpan);
    }
    li.appendChild(meta);

    const pre = document.createElement("pre");
    pre.className = "chat-body";
    if (streaming) {
        pre.classList.add("streaming");
    }
    pre.textContent = content || "";
    li.appendChild(pre);

    chatLog().appendChild(li);
    scrollToBottom();
    return { li, pre };
}

function scrollToBottom() {
    const main = document.querySelector(".chat-main");
    if (main) {
        main.scrollTop = main.scrollHeight;
    }
}

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------

async function ensureSession() {
    if (state.sessionId) {
        return state.sessionId;
    }
    const session = await api("POST", "/api/sessions", {
        title: "chat session",
    });
    state.sessionId = session.session_id;
    setSessionLabel(session.session_id);
    attachEventStream(session.session_id);
    document.getElementById("button-freeze").disabled = false;
    return session.session_id;
}

function setSessionLabel(sessionId) {
    const label = document.getElementById("chat-session-label");
    if (label) {
        label.textContent = sessionId ? `session: ${shortId(sessionId)}` : "";
    }
}

function shortId(id) {
    return id.split("-")[0];
}

async function startNewSession() {
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
    state.sessionId = null;
    state.streamingAssistantEl = null;
    state.streamingAssistantText = null;
    chatLog().innerHTML = "";
    const hint = emptyHint();
    if (hint) {
        hint.removeAttribute("hidden");
    }
    document.getElementById("button-freeze").disabled = true;
    setSessionLabel(null);
    flash("Started a new session");
}

// ---------------------------------------------------------------------------
// SSE streaming
// ---------------------------------------------------------------------------

function attachEventStream(sessionId) {
    const url = `/api/sessions/${sessionId}/events?follow=true&from_seq=0`;
    const source = new EventSource(url);
    state.eventSource = source;

    source.addEventListener("turn", (ev) => onTurnEvent(JSON.parse(ev.data)));
    source.addEventListener("agent_chunk", (ev) =>
        onAgentChunk(JSON.parse(ev.data)),
    );
    source.addEventListener("agent_turn", (ev) =>
        onAgentTurn(JSON.parse(ev.data)),
    );
    source.addEventListener("agent_error", (ev) =>
        onAgentError(JSON.parse(ev.data)),
    );
    source.onerror = () => {
        // Browsers reconnect automatically; no need to bother the user
        // unless the stream is dead long enough that flash() would help.
    };
}

function onTurnEvent(event) {
    const payload = event.payload || {};
    if (payload.role !== "user") {
        return;
    }
    const memoryUsed = payload.memory_used === true;
    appendMessage({
        role: "user",
        content: payload.content || "",
        badge: memoryUsed ? "memory: on" : null,
    });
    // Pre-create the assistant bubble so streaming deltas have a target.
    const placeholder = appendMessage({
        role: "assistant",
        content: "",
        streaming: true,
    });
    state.streamingAssistantEl = placeholder.pre;
    state.streamingAssistantText = "";
}

function onAgentChunk(event) {
    const payload = event.payload || {};
    if (payload.chunk_type !== "text_delta") {
        return;
    }
    const text = payload.text || "";
    if (!text) {
        return;
    }
    if (state.streamingAssistantEl === null) {
        const placeholder = appendMessage({
            role: "assistant",
            content: "",
            streaming: true,
        });
        state.streamingAssistantEl = placeholder.pre;
        state.streamingAssistantText = "";
    }
    state.streamingAssistantText += text;
    state.streamingAssistantEl.textContent = state.streamingAssistantText;
    scrollToBottom();
}

function onAgentTurn(event) {
    const payload = event.payload || {};
    const finalText = payload.content || state.streamingAssistantText || "";
    if (state.streamingAssistantEl !== null) {
        state.streamingAssistantEl.textContent = finalText;
        state.streamingAssistantEl.classList.remove("streaming");
    } else {
        appendMessage({ role: "assistant", content: finalText });
    }
    state.streamingAssistantEl = null;
    state.streamingAssistantText = null;
    state.submitting = false;
    document.getElementById("chat-send").disabled = false;
}

function onAgentError(event) {
    const payload = event.payload || {};
    appendMessage({
        role: "error",
        content: payload.error || "agent error",
    });
    if (state.streamingAssistantEl !== null) {
        state.streamingAssistantEl.classList.remove("streaming");
    }
    state.streamingAssistantEl = null;
    state.streamingAssistantText = null;
    state.submitting = false;
    document.getElementById("chat-send").disabled = false;
}

// ---------------------------------------------------------------------------
// Form handlers
// ---------------------------------------------------------------------------

async function onSubmit(ev) {
    ev.preventDefault();
    if (state.submitting) {
        return;
    }
    const input = document.getElementById("chat-input");
    const prompt = input.value.trim();
    if (!prompt) {
        return;
    }
    state.submitting = true;
    document.getElementById("chat-send").disabled = true;
    try {
        const sessionId = await ensureSession();
        await api(
            "POST",
            `/api/sessions/${sessionId}/agent-runs`,
            { prompt },
        );
        input.value = "";
    } catch (err) {
        appendMessage({ role: "error", content: String(err.message || err) });
        state.submitting = false;
        document.getElementById("chat-send").disabled = false;
    }
}

async function onFreeze() {
    if (!state.sessionId) {
        return;
    }
    try {
        const version = await api(
            "POST",
            `/api/sessions/${state.sessionId}/freeze`,
            {},
        );
        flash(`Frozen v${version.version_id ? shortId(version.version_id) : ""}`);
    } catch (err) {
        flash(String(err.message || err));
    }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function boot() {
    document.getElementById("chat-form").addEventListener("submit", onSubmit);
    document
        .getElementById("button-new-session")
        .addEventListener("click", startNewSession);
    document.getElementById("button-freeze").addEventListener("click", onFreeze);

    const input = document.getElementById("chat-input");
    input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            document
                .getElementById("chat-form")
                .dispatchEvent(new Event("submit", { cancelable: true }));
        }
    });
}

boot();
