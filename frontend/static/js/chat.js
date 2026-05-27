// stratoclave-atelier chat (Stages G + H + J): vanilla ES module, no build step.
//
// Drives the single-page chat at "/" against:
//   POST /api/sessions                          -> create root session
//   POST /api/sessions/{id}/agent-runs          -> kick off an agent run
//   GET  /api/sessions/{id}/events?follow=true  -> SSE stream
//   POST /api/sessions/{id}/freeze              -> freeze whole session
//   POST /api/sessions/{id}/branch              -> Stage J: freeze + auto-name + fork
//   GET  /api/groups/{id}/fork-graph            -> Stage J: DAG sidebar
//
// Stage J adds branch-aware navigation: the URL carries ``?session=<id>``,
// the header offers a "Fork now" affordance, each turn has a hover
// "Branch from here" button, and a right-side SVG renders the DAG with
// clickable nodes for cross-branch jumps. Edge memos are stored in
// localStorage (``atelier:fork-edge-memos``) -- non-critical UI hints, no
// server round-trip.

const EDGE_MEMO_KEY = "atelier:fork-edge-memos";

const state = {
    sessionId: null,
    eventSource: null,
    streamingAssistantEl: null,
    streamingAssistantText: null,
    submitting: false,
    backends: [],
    defaultBackend: null,
    selectedBackend: null,
    /**
     * Stage J: lightweight session metadata cache, indexed by id, used by
     * the breadcrumb + DAG. Populated lazily from /api/sessions/{id}.
     */
    sessionsCache: new Map(),
    /**
     * Stage J: the latest fork-graph payload for the current group. ``null``
     * means "no group / not yet loaded".
     */
    forkGraph: null,
    pendingBranchSeq: null,
    pendingMemoEdge: null,
    /** Map of "<parent>:<child>" -> memo string, mirrored to localStorage. */
    edgeMemos: loadEdgeMemos(),
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
// Render helpers (chat log)
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

function appendMessage({ role, content, streaming = false, badge = null, seq = null }) {
    hideEmptyHint();
    const li = document.createElement("li");
    li.className = `chat-message role-${role}`;
    li.dataset.role = role;
    if (seq !== null && seq !== undefined) {
        li.dataset.seq = String(seq);
    }

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

    // Stage J: per-turn hover "Branch from here" button. Only meaningful
    // when the turn has a recorded seq.
    if (seq !== null && seq !== undefined && (role === "user" || role === "assistant")) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "branch-here";
        btn.dataset.testid = "branch-here";
        btn.dataset.seq = String(seq);
        btn.title = `Branch from this turn (seq ${seq})`;
        btn.textContent = "Branch from here";
        btn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            openBranchConfirm(seq);
        });
        li.appendChild(btn);
    }

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
    const body = { title: "chat session" };
    if (state.selectedBackend) {
        body.agent_backend = state.selectedBackend;
    }
    const session = await api("POST", "/api/sessions", body);
    setActiveSession(session, { pushHistory: true });
    return session.session_id;
}

async function setActiveSession(session, { pushHistory = false } = {}) {
    state.sessionId = session.session_id;
    state.sessionsCache.set(session.session_id, session);
    setSessionLabel(session.session_id, session.agent_backend);
    document.getElementById("button-freeze").disabled = false;
    document.getElementById("button-branch").disabled = false;
    lockBackendPicker(true);

    if (pushHistory) {
        const url = new URL(window.location.href);
        url.searchParams.set("session", session.session_id);
        window.history.pushState(
            { sessionId: session.session_id },
            "",
            url.toString(),
        );
    }

    chatLog().innerHTML = "";
    state.streamingAssistantEl = null;
    state.streamingAssistantText = null;

    // Hydrate previous turns (if the session has any) before live-tailing.
    try {
        await hydrateSessionTurns(session.session_id);
    } catch (err) {
        // Hydration failure shouldn't break the chat; we just lose the
        // historical messages and proceed to live-tail.
    }
    attachEventStream(session.session_id);
    await renderBreadcrumb(session);
    await refreshForkGraph();
}

async function hydrateSessionTurns(sessionId) {
    // Use the SSE replay endpoint with follow=false to enumerate past
    // events; we render every "turn" event and skip control kinds.
    const resp = await fetch(
        `/api/sessions/${sessionId}/events?follow=false&from_seq=0`,
    );
    if (!resp.ok) {
        return;
    }
    const text = await resp.text();
    // Each SSE record is two lines (event: <kind>\n\ndata: <json>\n\n).
    // We're not following; the body is finite, so a quick line parse works.
    const lines = text.split("\n");
    let currentEvent = null;
    for (const line of lines) {
        if (line.startsWith("event:")) {
            currentEvent = line.slice(6).trim();
        } else if (line.startsWith("data:") && currentEvent) {
            const payload = JSON.parse(line.slice(5).trim());
            if (currentEvent === "turn") {
                renderHistoricalTurn(payload);
            }
            currentEvent = null;
        }
    }
}

function renderHistoricalTurn(event) {
    const payload = event.payload || {};
    const role = payload.role === "user" ? "user" : "assistant";
    appendMessage({
        role,
        content: payload.content || "",
        seq: event.seq,
    });
}

function setSessionLabel(sessionId, backend) {
    const label = document.getElementById("chat-session-label");
    if (!label) {
        return;
    }
    if (!sessionId) {
        label.textContent = "";
        return;
    }
    const tail = backend ? ` · ${backend}` : "";
    label.textContent = `session: ${shortId(sessionId)}${tail}`;
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
    document.getElementById("button-branch").disabled = true;
    setSessionLabel(null);
    lockBackendPicker(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("session");
    window.history.pushState({ sessionId: null }, "", url.toString());
    clearBreadcrumb();
    flash("Started a new session");
}

async function navigateToSession(sessionId) {
    if (state.sessionId === sessionId) {
        return;
    }
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
    state.sessionId = null;
    let session = state.sessionsCache.get(sessionId);
    if (!session) {
        try {
            session = await api("GET", `/api/sessions/${sessionId}`);
        } catch (err) {
            flash(`Could not open session ${shortId(sessionId)}`);
            return;
        }
    }
    await setActiveSession(session, { pushHistory: true });
}

// ---------------------------------------------------------------------------
// Stage H backend picker
// ---------------------------------------------------------------------------

async function loadBackends() {
    const select = document.getElementById("chat-backend");
    if (!select) {
        return;
    }
    let info;
    try {
        info = await api("GET", "/api/agent/backends");
    } catch (err) {
        select.innerHTML = "";
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "(unavailable)";
        select.appendChild(opt);
        select.disabled = true;
        return;
    }
    state.backends = info.backends || [];
    state.defaultBackend = info.default || null;

    select.innerHTML = "";
    if (state.backends.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "(none configured)";
        select.appendChild(opt);
        select.disabled = true;
        state.selectedBackend = null;
        return;
    }
    for (const b of state.backends) {
        const opt = document.createElement("option");
        opt.value = b.name;
        const suffix = b.ready ? "" : " (cwd missing)";
        opt.textContent = `${b.name}${suffix}`;
        opt.disabled = !b.ready;
        select.appendChild(opt);
    }
    const initial =
        state.defaultBackend && state.backends.some((b) => b.name === state.defaultBackend)
            ? state.defaultBackend
            : state.backends.find((b) => b.ready)?.name || state.backends[0].name;
    select.value = initial;
    state.selectedBackend = initial;
    select.disabled = false;
    select.addEventListener("change", () => {
        state.selectedBackend = select.value || null;
    });
}

function lockBackendPicker(locked) {
    const select = document.getElementById("chat-backend");
    if (select) {
        select.disabled = locked || state.backends.length === 0;
    }
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
    source.onerror = () => {};
}

function onTurnEvent(event) {
    const payload = event.payload || {};
    if (payload.role !== "user") {
        return;
    }
    // Ignore replays of turns we've already rendered during hydration.
    if (chatLog().querySelector(`[data-seq="${event.seq}"]`)) {
        return;
    }
    const memoryUsed = payload.memory_used === true;
    appendMessage({
        role: "user",
        content: payload.content || "",
        badge: memoryUsed ? "memory: on" : null,
        seq: event.seq,
    });
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
        if (event.seq !== undefined && event.seq !== null) {
            state.streamingAssistantEl.parentElement.dataset.seq = String(event.seq);
        }
    } else {
        appendMessage({ role: "assistant", content: finalText, seq: event.seq });
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
// Stage J: branch flow (Fork now header + per-turn Branch from here)
// ---------------------------------------------------------------------------

function openBranchConfirm(seq) {
    if (!state.sessionId) {
        return;
    }
    state.pendingBranchSeq = seq;
    const dlg = document.getElementById("branch-confirm");
    document.getElementById("branch-confirm-seq").textContent =
        seq === null ? "(end)" : String(seq);
    document.getElementById("branch-confirm-title").value = "";
    if (typeof dlg.showModal === "function") {
        dlg.showModal();
    } else {
        dlg.setAttribute("open", "");
    }
}

async function confirmBranch() {
    const dlg = document.getElementById("branch-confirm");
    const titleInput = document.getElementById("branch-confirm-title");
    const title = titleInput.value.trim();
    const seq = state.pendingBranchSeq;
    closeDialog(dlg);
    state.pendingBranchSeq = null;
    if (!state.sessionId) {
        return;
    }
    const body = {};
    if (seq !== null && seq !== undefined) {
        body.end_seq = seq;
    }
    if (title) {
        body.title = title;
    }
    try {
        const result = await api(
            "POST",
            `/api/sessions/${state.sessionId}/branch`,
            body,
        );
        flash(
            result.auto_named
                ? `Branched: ${result.child.title}`
                : `Branched (manual): ${result.child.title}`,
        );
        await refreshForkGraph();
        await navigateToSession(result.child.session_id);
    } catch (err) {
        flash(String(err.message || err));
    }
}

function cancelBranch() {
    state.pendingBranchSeq = null;
    closeDialog(document.getElementById("branch-confirm"));
}

function closeDialog(dlg) {
    if (typeof dlg.close === "function") {
        dlg.close();
    } else {
        dlg.removeAttribute("open");
    }
}

// ---------------------------------------------------------------------------
// Stage J: breadcrumb (parent ancestry)
// ---------------------------------------------------------------------------

function clearBreadcrumb() {
    const nav = document.getElementById("chat-breadcrumb");
    nav.innerHTML = "";
    nav.setAttribute("hidden", "");
}

async function renderBreadcrumb(session) {
    const nav = document.getElementById("chat-breadcrumb");
    nav.innerHTML = "";
    const chain = await collectAncestry(session);
    if (chain.length <= 1) {
        nav.removeAttribute("hidden");
        const cur = document.createElement("span");
        cur.className = "crumb-current";
        cur.textContent = session.title || shortId(session.session_id);
        nav.appendChild(cur);
        return;
    }
    nav.removeAttribute("hidden");
    chain.forEach((s, idx) => {
        if (idx > 0) {
            const sep = document.createElement("span");
            sep.className = "crumb-sep";
            sep.textContent = "›";
            nav.appendChild(sep);
        }
        if (idx === chain.length - 1) {
            const cur = document.createElement("span");
            cur.className = "crumb-current";
            cur.textContent = s.title || shortId(s.session_id);
            nav.appendChild(cur);
        } else {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "crumb";
            btn.dataset.testid = "crumb-link";
            btn.dataset.sessionId = s.session_id;
            btn.textContent = s.title || shortId(s.session_id);
            btn.addEventListener("click", () => navigateToSession(s.session_id));
            nav.appendChild(btn);
        }
    });
}

async function collectAncestry(session) {
    const chain = [session];
    let cursor = session;
    while (cursor && cursor.parent_session_id) {
        let parent = state.sessionsCache.get(cursor.parent_session_id);
        if (!parent) {
            try {
                parent = await api("GET", `/api/sessions/${cursor.parent_session_id}`);
                state.sessionsCache.set(parent.session_id, parent);
            } catch (err) {
                break;
            }
        }
        chain.unshift(parent);
        cursor = parent;
    }
    return chain;
}

// ---------------------------------------------------------------------------
// Stage J: Fork DAG sidebar (SVG layout)
// ---------------------------------------------------------------------------

async function refreshForkGraph() {
    const dagEmpty = document.getElementById("dag-empty");
    const svg = document.getElementById("dag-svg");
    if (!state.sessionId) {
        state.forkGraph = null;
        dagEmpty.removeAttribute("hidden");
        svg.setAttribute("hidden", "");
        return;
    }
    const session = state.sessionsCache.get(state.sessionId);
    const groupId = session ? session.group_id : null;
    if (!groupId) {
        // No group: build a synthetic single-node graph from the
        // ancestry. The session-by-session endpoint suffices.
        state.forkGraph = await buildSyntheticGraph(state.sessionId);
    } else {
        try {
            state.forkGraph = await api("GET", `/api/groups/${groupId}/fork-graph`);
        } catch (err) {
            state.forkGraph = await buildSyntheticGraph(state.sessionId);
        }
    }
    if (!state.forkGraph || state.forkGraph.nodes.length === 0) {
        dagEmpty.removeAttribute("hidden");
        svg.setAttribute("hidden", "");
        return;
    }
    if (state.forkGraph.nodes.length <= 1 && state.forkGraph.edges.length === 0) {
        dagEmpty.removeAttribute("hidden");
        svg.setAttribute("hidden", "");
        return;
    }
    dagEmpty.setAttribute("hidden", "");
    svg.removeAttribute("hidden");
    renderDag(state.forkGraph);
}

async function buildSyntheticGraph(sessionId) {
    const chain = [];
    let cursor = state.sessionsCache.get(sessionId);
    if (!cursor) {
        try {
            cursor = await api("GET", `/api/sessions/${sessionId}`);
            state.sessionsCache.set(cursor.session_id, cursor);
        } catch (err) {
            return { nodes: [], edges: [] };
        }
    }
    chain.unshift(cursor);
    while (cursor.parent_session_id) {
        let parent = state.sessionsCache.get(cursor.parent_session_id);
        if (!parent) {
            try {
                parent = await api("GET", `/api/sessions/${cursor.parent_session_id}`);
                state.sessionsCache.set(parent.session_id, parent);
            } catch (err) {
                break;
            }
        }
        chain.unshift(parent);
        cursor = parent;
    }
    const nodes = chain.map((s) => ({
        session_id: s.session_id,
        title: s.title,
        status: s.status,
        parent_session_id: s.parent_session_id,
        parent_version_id: s.parent_version_id,
        fork_seq: s.fork_seq,
        versions: [],
    }));
    const edges = [];
    for (let i = 1; i < chain.length; i++) {
        edges.push({
            parent_session_id: chain[i - 1].session_id,
            child_session_id: chain[i].session_id,
            via_version_id: chain[i].parent_version_id,
            fork_seq: chain[i].fork_seq,
        });
    }
    return { nodes, edges };
}

function renderDag(graph) {
    const svg = document.getElementById("dag-svg");
    svg.innerHTML = "";
    const layout = layoutDag(graph);
    const padding = 14;
    const nodeWidth = 160;
    const nodeHeight = 32;
    const rowHeight = 64;
    const colWidth = nodeWidth + 28;

    const cols = Math.max(...layout.map((n) => n.col), 0) + 1;
    const rows = Math.max(...layout.map((n) => n.row), 0) + 1;
    const widthPx = padding * 2 + cols * colWidth + nodeWidth;
    const heightPx = padding * 2 + rows * rowHeight + nodeHeight;
    svg.setAttribute(
        "viewBox",
        `0 0 ${Math.max(widthPx, 260)} ${Math.max(heightPx, 100)}`,
    );
    svg.setAttribute("width", String(Math.max(widthPx, 260)));
    svg.setAttribute("height", String(Math.max(heightPx, 100)));

    const positions = new Map();
    for (const n of layout) {
        const x = padding + n.col * colWidth;
        const y = padding + n.row * rowHeight;
        positions.set(n.session_id, { x, y });
    }

    // Edges first (under nodes)
    for (const edge of graph.edges) {
        const a = positions.get(edge.parent_session_id);
        const b = positions.get(edge.child_session_id);
        if (!a || !b) continue;
        const x1 = a.x + nodeWidth / 2;
        const y1 = a.y + nodeHeight;
        const x2 = b.x + nodeWidth / 2;
        const y2 = b.y;
        const d = `M${x1},${y1} C${x1},${y1 + 18} ${x2},${y2 - 18} ${x2},${y2}`;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d);
        path.setAttribute("class", "dag-edge");
        svg.appendChild(path);

        const memoKey = `${edge.parent_session_id}:${edge.child_session_id}`;
        const memo = state.edgeMemos[memoKey] || "";
        const labelText = memo || `seq ${edge.fork_seq ?? ""}`;
        const tx = (x1 + x2) / 2;
        const ty = (y1 + y2) / 2;
        const label = document.createElementNS(
            "http://www.w3.org/2000/svg",
            "text",
        );
        label.setAttribute("x", String(tx));
        label.setAttribute("y", String(ty));
        label.setAttribute("class", "dag-edge-label");
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("dominant-baseline", "middle");
        label.dataset.testid = "dag-edge-label";
        label.dataset.parent = edge.parent_session_id;
        label.dataset.child = edge.child_session_id;
        label.textContent = labelText;
        label.addEventListener("click", () =>
            openEdgeMemoDialog(edge.parent_session_id, edge.child_session_id),
        );
        svg.appendChild(label);
    }

    // Nodes
    for (const n of layout) {
        const pos = positions.get(n.session_id);
        const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
        g.setAttribute("class", "dag-node");
        g.dataset.testid = "dag-node";
        g.dataset.sessionId = n.session_id;
        if (n.session_id === state.sessionId) {
            g.classList.add("current");
        }

        const rect = document.createElementNS(
            "http://www.w3.org/2000/svg",
            "rect",
        );
        rect.setAttribute("x", String(pos.x));
        rect.setAttribute("y", String(pos.y));
        rect.setAttribute("width", String(nodeWidth));
        rect.setAttribute("height", String(nodeHeight));
        rect.setAttribute("rx", "5");
        rect.setAttribute("ry", "5");
        g.appendChild(rect);

        const text = document.createElementNS(
            "http://www.w3.org/2000/svg",
            "text",
        );
        text.setAttribute("x", String(pos.x + 10));
        text.setAttribute("y", String(pos.y + nodeHeight / 2 + 4));
        const title = (n.title || shortId(n.session_id)).slice(0, 22);
        text.textContent = title;
        g.appendChild(text);

        g.addEventListener("click", () => navigateToSession(n.session_id));
        svg.appendChild(g);
    }
}

function layoutDag(graph) {
    // Topological levelisation: roots at row 0, children one row below
    // their parent. Multiple siblings share the row but slot into
    // increasing columns. Good enough for shallow DAGs (< ~20 nodes).
    const byParent = new Map();
    const byId = new Map();
    for (const n of graph.nodes) {
        byId.set(n.session_id, n);
    }
    for (const e of graph.edges) {
        if (!byParent.has(e.parent_session_id)) {
            byParent.set(e.parent_session_id, []);
        }
        byParent.get(e.parent_session_id).push(e.child_session_id);
    }
    const childIds = new Set(graph.edges.map((e) => e.child_session_id));
    const roots = graph.nodes.filter((n) => !childIds.has(n.session_id));
    if (roots.length === 0 && graph.nodes.length > 0) {
        roots.push(graph.nodes[0]);
    }
    const result = [];
    let nextCol = 0;
    function visit(nodeId, row) {
        const node = byId.get(nodeId);
        if (!node) return;
        const col = nextCol++;
        result.push({ ...node, row, col });
        const kids = byParent.get(nodeId) || [];
        for (const k of kids) {
            visit(k, row + 1);
        }
    }
    for (const r of roots) {
        visit(r.session_id, 0);
    }
    // Catch any orphaned nodes (data race / missing edge)
    for (const n of graph.nodes) {
        if (!result.find((r) => r.session_id === n.session_id)) {
            result.push({ ...n, row: 0, col: nextCol++ });
        }
    }
    return result;
}

// ---------------------------------------------------------------------------
// Stage J: edge memos (localStorage)
// ---------------------------------------------------------------------------

function loadEdgeMemos() {
    try {
        const raw = window.localStorage.getItem(EDGE_MEMO_KEY);
        if (!raw) {
            return {};
        }
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {};
    } catch (err) {
        return {};
    }
}

function persistEdgeMemos() {
    try {
        window.localStorage.setItem(
            EDGE_MEMO_KEY,
            JSON.stringify(state.edgeMemos),
        );
    } catch (err) {
        // Quota / private mode -- non-critical.
    }
}

function openEdgeMemoDialog(parentId, childId) {
    state.pendingMemoEdge = { parentId, childId };
    const dlg = document.getElementById("edge-memo");
    const ta = document.getElementById("edge-memo-text");
    const key = `${parentId}:${childId}`;
    ta.value = state.edgeMemos[key] || "";
    if (typeof dlg.showModal === "function") {
        dlg.showModal();
    } else {
        dlg.setAttribute("open", "");
    }
}

function saveEdgeMemo() {
    const dlg = document.getElementById("edge-memo");
    const ta = document.getElementById("edge-memo-text");
    const edge = state.pendingMemoEdge;
    closeDialog(dlg);
    state.pendingMemoEdge = null;
    if (!edge) {
        return;
    }
    const key = `${edge.parentId}:${edge.childId}`;
    const value = ta.value.trim();
    if (value) {
        state.edgeMemos[key] = value;
    } else {
        delete state.edgeMemos[key];
    }
    persistEdgeMemos();
    if (state.forkGraph) {
        renderDag(state.forkGraph);
    }
}

function cancelEdgeMemo() {
    state.pendingMemoEdge = null;
    closeDialog(document.getElementById("edge-memo"));
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
    document
        .getElementById("button-branch")
        .addEventListener("click", () => openBranchConfirm(null));
    document
        .getElementById("branch-confirm-ok")
        .addEventListener("click", confirmBranch);
    document
        .getElementById("branch-confirm-cancel")
        .addEventListener("click", cancelBranch);
    document
        .getElementById("edge-memo-save")
        .addEventListener("click", saveEdgeMemo);
    document
        .getElementById("edge-memo-cancel")
        .addEventListener("click", cancelEdgeMemo);
    document
        .getElementById("button-dag-refresh")
        .addEventListener("click", refreshForkGraph);

    const input = document.getElementById("chat-input");
    input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            document
                .getElementById("chat-form")
                .dispatchEvent(new Event("submit", { cancelable: true }));
        }
    });

    window.addEventListener("popstate", async (ev) => {
        const params = new URLSearchParams(window.location.search);
        const sessionId = params.get("session");
        if (!sessionId) {
            await startNewSession();
            return;
        }
        await navigateToSession(sessionId);
    });

    loadBackends();

    // Stage J: deep-link via ?session=<id>. Picking up a pre-existing
    // session preserves the chat after a hard refresh.
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get("session");
    if (sessionId) {
        navigateToSession(sessionId).catch(() => {
            // If the link is bogus we fall through to "new session" UX.
        });
    }
}

boot();

// Exposed for unit/E2E tests; keeps the production module self-contained.
if (typeof window !== "undefined") {
    window.__atelier = {
        state,
        layoutDag,
        loadEdgeMemos,
    };
}
