// stratoclave-atelier SPA: vanilla ES modules, no build step.
//
// Wires the Stage B/C/D REST + WS endpoints to a four-panel UI:
// 1. Groups (create + list)
// 2. Sessions (create + list, filtered by active group)
// 3. Turns (WebSocket ingest + freeze + version list)
// 4. Fork graph (SVG drawn from /api/groups/{id}/fork-graph)
//
// State is intentionally kept as a flat module-level object: this is a
// walking-skeleton SPA, not a framework.

const state = {
    activeGroupId: null,
    activeGroupName: null,
    activeSessionId: null,
    activeSessionTitle: null,
    sessions: [],
    versions: [],
    turns: [],
    ws: null,
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
    setTimeout(() => bar.classList.remove("show"), 1500);
}

// ---------------------------------------------------------------------------
// Groups
// ---------------------------------------------------------------------------

async function loadGroups() {
    const groups = await api("GET", "/api/groups");
    const list = document.getElementById("list-groups");
    list.innerHTML = "";
    for (const g of groups) {
        const li = document.createElement("li");
        li.textContent = g.name;
        li.dataset.testid = "group-item";
        li.dataset.groupId = g.group_id;
        if (g.group_id === state.activeGroupId) li.classList.add("active");
        li.addEventListener("click", () => selectGroup(g));
        list.appendChild(li);
    }
}

async function selectGroup(group) {
    state.activeGroupId = group.group_id;
    state.activeGroupName = group.name;
    document.getElementById("active-group-label").textContent = `· ${group.name}`;
    await loadGroups();
    await loadSessions();
    await loadForkGraph();
}

async function createGroup(event) {
    event.preventDefault();
    const form = event.target;
    const name = form.elements.name.value.trim();
    if (!name) return;
    const group = await api("POST", "/api/groups", { name, description: null });
    form.reset();
    await selectGroup(group);
    flash(`group "${group.name}" created`);
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

async function loadSessions() {
    const params = state.activeGroupId
        ? `?group_id=${encodeURIComponent(state.activeGroupId)}`
        : "";
    const sessions = await api("GET", `/api/sessions${params}`);
    state.sessions = sessions;
    const list = document.getElementById("list-sessions");
    list.innerHTML = "";
    for (const s of sessions) {
        const li = document.createElement("li");
        li.textContent = s.title;
        li.dataset.testid = "session-item";
        li.dataset.sessionId = s.session_id;
        if (s.session_id === state.activeSessionId) li.classList.add("active");
        li.addEventListener("click", () => selectSession(s));
        list.appendChild(li);
    }
}

async function selectSession(session) {
    state.activeSessionId = session.session_id;
    state.activeSessionTitle = session.title;
    document.getElementById("active-session-label").textContent = `· ${session.title}`;
    document.getElementById("active-graph-label").textContent = state.activeGroupName
        ? `· ${state.activeGroupName}`
        : `· ${session.title}`;
    document.getElementById("form-send-turn").hidden = false;
    document.getElementById("button-freeze").hidden = false;
    await loadSessions();
    await loadTimeline();
    await loadVersions();
    await loadForkGraph();
    openIngestSocket();
}

async function createSession(event) {
    event.preventDefault();
    const form = event.target;
    const title = form.elements.title.value.trim();
    if (!title) return;
    const payload = { title };
    if (state.activeGroupId) payload.group_id = state.activeGroupId;
    const session = await api("POST", "/api/sessions", payload);
    form.reset();
    await selectSession(session);
    flash(`session "${session.title}" created`);
}

// ---------------------------------------------------------------------------
// Turns + WebSocket ingest
// ---------------------------------------------------------------------------

async function loadTimeline() {
    if (!state.activeSessionId) return;
    const events = [];
    // Stage C SSE replay: we just consume it as plain HTTP for the SPA
    // (browsers can stream SSE via EventSource, but for a static replay
    // a single fetch suffices and is easier to test).
    const resp = await fetch(
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/events`
    );
    if (!resp.ok) {
        throw new Error(`SSE replay failed: ${resp.status}`);
    }
    const text = await resp.text();
    for (const block of text.split("\n\n")) {
        const dataLine = block.split("\n").find((l) => l.startsWith("data: "));
        if (!dataLine) continue;
        try {
            events.push(JSON.parse(dataLine.slice("data: ".length)));
        } catch {
            // skip malformed
        }
    }
    state.turns = events;
    renderTimeline();
}

function renderTimeline() {
    const list = document.getElementById("list-turns");
    list.innerHTML = "";
    for (const ev of state.turns) {
        const li = document.createElement("li");
        li.dataset.kind = ev.kind;
        li.dataset.testid = "turn-item";
        const summary = ev.kind === "turn"
            ? `${ev.payload.role || "?"}: ${ev.payload.content || ""}`
            : `[${ev.kind}] ${JSON.stringify(ev.payload)}`;
        li.textContent = `#${ev.seq} ${summary}`;
        list.appendChild(li);
    }
}

function openIngestSocket() {
    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }
    if (!state.activeSessionId) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/api/sessions/${encodeURIComponent(
        state.activeSessionId
    )}/ingest`;
    const ws = new WebSocket(url);
    ws.addEventListener("message", async (e) => {
        try {
            const ack = JSON.parse(e.data);
            if (ack && ack.seq !== undefined) {
                await loadTimeline();
            }
        } catch {
            // ignore non-json frames
        }
    });
    ws.addEventListener("close", () => {
        if (state.ws === ws) state.ws = null;
    });
    state.ws = ws;
}

async function sendTurn(event) {
    event.preventDefault();
    if (!state.activeSessionId) return;
    const form = event.target;
    const role = form.elements.role.value.trim() || "user";
    const content = form.elements.content.value;
    const payload = { kind: "turn", role, content };

    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
        await waitForOpenSocket();
    }
    state.ws.send(JSON.stringify(payload));
    form.elements.content.value = "";
    flash("turn sent");
}

function waitForOpenSocket() {
    return new Promise((resolve, reject) => {
        if (!state.ws) return reject(new Error("no socket"));
        if (state.ws.readyState === WebSocket.OPEN) return resolve();
        state.ws.addEventListener("open", () => resolve(), { once: true });
        state.ws.addEventListener("error", () => reject(new Error("ws error")), {
            once: true,
        });
    });
}

// ---------------------------------------------------------------------------
// Versions + freeze
// ---------------------------------------------------------------------------

async function loadVersions() {
    if (!state.activeSessionId) return;
    const versions = await api(
        "GET",
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/versions`
    );
    state.versions = versions;
    const list = document.getElementById("list-versions");
    list.innerHTML = "";
    for (const v of versions) {
        const li = document.createElement("li");
        li.dataset.testid = "version-item";
        li.dataset.versionId = v.version_id;
        const label = v.label || `v${v.start_seq}-${v.end_seq}`;
        li.textContent = `${label} (turns ${v.start_seq}..${v.end_seq}, ${v.byte_size}B)`;
        list.appendChild(li);
    }
}

async function freezeWholeSession() {
    if (!state.activeSessionId) return;
    const label = `frozen at ${new Date().toISOString()}`;
    await api(
        "POST",
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/freeze`,
        { label }
    );
    flash("session frozen");
    await loadTimeline();
    await loadVersions();
    await loadForkGraph();
}

// ---------------------------------------------------------------------------
// Fork graph
// ---------------------------------------------------------------------------

async function loadForkGraph() {
    let graph = null;
    if (state.activeGroupId) {
        graph = await api(
            "GET",
            `/api/groups/${encodeURIComponent(state.activeGroupId)}/fork-graph`
        );
    } else if (state.activeSessionId) {
        graph = await api(
            "GET",
            `/api/sessions/${encodeURIComponent(state.activeSessionId)}/fork-graph`
        );
    } else {
        graph = { nodes: [], edges: [] };
    }
    renderForkGraph(graph);
}

function renderForkGraph(graph) {
    const svg = document.getElementById("fork-graph-svg");
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const NS = "http://www.w3.org/2000/svg";
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];

    // Simple deterministic layout: sort by created order (already returned
    // sorted by store), assign columns by parent depth, rows by sibling
    // index.
    const depthByNode = new Map();
    for (const n of nodes) {
        const parent = n.parent_session_id;
        const parentDepth = parent && depthByNode.has(parent) ? depthByNode.get(parent) : -1;
        depthByNode.set(n.session_id, parentDepth + 1);
    }
    const rowByDepth = new Map();
    const positions = new Map();
    for (const n of nodes) {
        const depth = depthByNode.get(n.session_id);
        const row = rowByDepth.get(depth) || 0;
        rowByDepth.set(depth, row + 1);
        const x = 60 + depth * 200;
        const y = 40 + row * 90;
        positions.set(n.session_id, { x, y });
    }

    for (const e of edges) {
        const from = positions.get(e.parent_session_id);
        const to = positions.get(e.child_session_id);
        if (!from || !to) continue;
        const path = document.createElementNS(NS, "path");
        const d = `M ${from.x + 80} ${from.y + 25} C ${from.x + 140} ${from.y + 25}, ${
            to.x - 60
        } ${to.y + 25}, ${to.x} ${to.y + 25}`;
        path.setAttribute("d", d);
        path.setAttribute("class", "graph-edge");
        svg.appendChild(path);

        const label = document.createElementNS(NS, "text");
        label.setAttribute("x", (from.x + 80 + to.x) / 2);
        label.setAttribute("y", (from.y + to.y) / 2 + 18);
        label.setAttribute("class", "graph-edge-label");
        label.textContent = `turn ${e.fork_seq}`;
        svg.appendChild(label);
    }

    for (const n of nodes) {
        const pos = positions.get(n.session_id);
        if (!pos) continue;
        const g = document.createElementNS(NS, "g");
        g.setAttribute("class", "graph-node");
        g.setAttribute("data-testid", "graph-node");
        g.setAttribute("data-session-id", n.session_id);

        const rect = document.createElementNS(NS, "rect");
        rect.setAttribute("x", pos.x);
        rect.setAttribute("y", pos.y);
        rect.setAttribute("width", 160);
        rect.setAttribute("height", 50);
        rect.setAttribute("rx", 6);
        const klass =
            n.versions && n.versions.length > 0 ? "graph-node-rect frozen" : "graph-node-rect";
        rect.setAttribute("class", klass);
        g.appendChild(rect);

        const title = document.createElementNS(NS, "text");
        title.setAttribute("x", pos.x + 8);
        title.setAttribute("y", pos.y + 18);
        title.setAttribute("class", "graph-node-label");
        title.textContent = n.title;
        g.appendChild(title);

        const meta = document.createElementNS(NS, "text");
        meta.setAttribute("x", pos.x + 8);
        meta.setAttribute("y", pos.y + 36);
        meta.setAttribute("class", "graph-node-label");
        const versionsText = n.versions && n.versions.length > 0
            ? `${n.versions.length} version${n.versions.length === 1 ? "" : "s"}`
            : "no versions";
        meta.textContent = `${n.status} · ${versionsText}`;
        g.appendChild(meta);

        svg.appendChild(g);
    }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
    document
        .getElementById("form-create-group")
        .addEventListener("submit", createGroup);
    document
        .getElementById("form-create-session")
        .addEventListener("submit", createSession);
    document.getElementById("form-send-turn").addEventListener("submit", sendTurn);
    document.getElementById("button-freeze").addEventListener("click", freezeWholeSession);
    await loadGroups();
});
