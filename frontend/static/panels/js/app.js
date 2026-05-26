// stratoclave-atelier SPA: vanilla ES modules, no build step.
//
// Wires the Stage B/C/D/F REST + WS + SSE endpoints to a four-panel UI:
// 1. Groups (create + list)
// 2. Sessions (create + list, filtered by active group)
// 3. Turns (WS ingest + SSE live tail + per-turn freeze + version list)
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
    eventSource: null,
    lastSeq: -1,
    rangeAnchorSeq: null,
    forkVersion: null,
    snapshotVersion: null,
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
    state.lastSeq = -1;
    state.rangeAnchorSeq = null;
    state.turns = [];
    document.getElementById("active-session-label").textContent = `· ${session.title}`;
    document.getElementById("active-graph-label").textContent = state.activeGroupName
        ? `· ${state.activeGroupName}`
        : `· ${session.title}`;
    document.getElementById("form-send-turn").hidden = false;
    document.getElementById("freeze-controls").hidden = false;
    updateRangeIndicator();
    await loadSessions();
    await loadTimeline();
    await loadVersions();
    await loadForkGraph();
    openIngestSocket();
    openLiveTail();
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
// Turns + WebSocket ingest + SSE live tail
// ---------------------------------------------------------------------------

async function loadTimeline() {
    if (!state.activeSessionId) return;
    const events = [];
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
    state.lastSeq = events.length === 0 ? -1 : events[events.length - 1].seq;
    renderTimeline();
}

function renderTimeline() {
    const list = document.getElementById("list-turns");
    list.innerHTML = "";
    for (const ev of state.turns) {
        const li = document.createElement("li");
        li.dataset.kind = ev.kind;
        li.dataset.testid = "turn-item";
        li.dataset.seq = String(ev.seq);
        if (ev.seq === state.rangeAnchorSeq) li.classList.add("range-anchor");
        const summary = ev.kind === "turn"
            ? `${ev.payload.role || "?"}: ${ev.payload.content || ""}`
            : `[${ev.kind}] ${JSON.stringify(ev.payload)}`;
        li.append(document.createTextNode(`#${ev.seq} ${summary} `));

        if (ev.kind === "turn") {
            const actions = document.createElement("span");
            actions.className = "turn-actions";

            const through = document.createElement("button");
            through.type = "button";
            through.dataset.testid = "freeze-through-button";
            through.textContent = "Freeze through";
            through.addEventListener("click", (e) => {
                e.stopPropagation();
                if (e.shiftKey) {
                    toggleRangeAnchor(ev.seq);
                } else if (state.rangeAnchorSeq !== null) {
                    completeRangeFreeze(ev.seq);
                } else {
                    freezeThroughSeq(ev.seq);
                }
            });
            actions.appendChild(through);

            li.appendChild(actions);
        }
        list.appendChild(li);
    }
}

function mergeIncomingEvent(ev) {
    if (typeof ev.seq !== "number") return;
    if (ev.seq <= state.lastSeq) return;
    state.turns.push(ev);
    state.lastSeq = ev.seq;
    renderTimeline();
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
    ws.addEventListener("close", () => {
        if (state.ws === ws) state.ws = null;
    });
    state.ws = ws;
}

function openLiveTail() {
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
    if (!state.activeSessionId) return;
    const fromSeq = state.lastSeq + 1;
    const url =
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/events` +
        `?from_seq=${fromSeq}`;
    let es;
    try {
        es = new EventSource(url);
    } catch {
        return;
    }
    es.addEventListener("message", async (e) => {
        try {
            const ev = JSON.parse(e.data);
            mergeIncomingEvent(ev);
            if (ev.kind === "freeze") {
                await loadVersions();
                await loadForkGraph();
            }
        } catch {
            // ignore
        }
    });
    es.addEventListener("error", () => {
        // Browsers retry SSE automatically. We close on session switch.
    });
    state.eventSource = es;
}

async function sendTurn(event) {
    event.preventDefault();
    if (!state.activeSessionId) return;
    const form = event.target;
    const role = form.elements.role.value.trim() || "user";
    const content = form.elements.content.value;
    const payload = { kind: "turn", role, content };

    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
        try {
            await waitForOpenSocket();
        } catch {
            // WS unavailable -- fall back to HTTP turn append.
            await api(
                "POST",
                `/api/sessions/${encodeURIComponent(state.activeSessionId)}/turns`,
                { role, content }
            );
            form.elements.content.value = "";
            flash("turn sent (HTTP)");
            return;
        }
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
// Versions + freeze (whole + per-turn + range)
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

        const row = document.createElement("div");
        row.className = "version-row";

        const head = document.createElement("span");
        const label = v.label || `v${v.start_seq}-${v.end_seq}`;
        head.textContent = `${label} (turns ${v.start_seq}..${v.end_seq}, ${v.byte_size}B)`;
        row.appendChild(head);

        const actions = document.createElement("div");
        actions.className = "version-actions";

        const forkBtn = document.createElement("button");
        forkBtn.type = "button";
        forkBtn.dataset.testid = "version-fork-button";
        forkBtn.textContent = "Fork";
        forkBtn.addEventListener("click", () => openForkDialog(v));
        actions.appendChild(forkBtn);

        const askBtn = document.createElement("button");
        askBtn.type = "button";
        askBtn.dataset.testid = "version-ask-button";
        askBtn.textContent = "Snapshot query";
        askBtn.addEventListener("click", () => openSnapshotDialog(v));
        actions.appendChild(askBtn);

        row.appendChild(actions);
        li.appendChild(row);
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

async function freezeThroughSeq(seq) {
    if (!state.activeSessionId) return;
    const label = `from #${seq} at ${new Date().toISOString()}`;
    await api(
        "POST",
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/freeze`,
        { start_seq: seq, label }
    );
    flash(`frozen through #${seq}`);
    await loadVersions();
    await loadForkGraph();
}

async function completeRangeFreeze(endSeq) {
    if (!state.activeSessionId) return;
    if (state.rangeAnchorSeq === null) return;
    const startSeq = Math.min(state.rangeAnchorSeq, endSeq);
    const finalEnd = Math.max(state.rangeAnchorSeq, endSeq);
    const label = `range #${startSeq}-#${finalEnd} at ${new Date().toISOString()}`;
    await api(
        "POST",
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/freeze`,
        { start_seq: startSeq, end_seq: finalEnd, label }
    );
    state.rangeAnchorSeq = null;
    updateRangeIndicator();
    flash(`frozen range #${startSeq}..#${finalEnd}`);
    renderTimeline();
    await loadVersions();
    await loadForkGraph();
}

function toggleRangeAnchor(seq) {
    state.rangeAnchorSeq = state.rangeAnchorSeq === seq ? null : seq;
    updateRangeIndicator();
    renderTimeline();
}

function cancelRange() {
    state.rangeAnchorSeq = null;
    updateRangeIndicator();
    renderTimeline();
}

function updateRangeIndicator() {
    const indicator = document.getElementById("range-indicator");
    const cancel = document.getElementById("button-cancel-range");
    if (state.rangeAnchorSeq === null) {
        indicator.hidden = true;
        cancel.hidden = true;
        indicator.textContent = "";
    } else {
        indicator.hidden = false;
        cancel.hidden = false;
        indicator.textContent = `range anchor: #${state.rangeAnchorSeq}`;
    }
}

// ---------------------------------------------------------------------------
// Fork dialog
// ---------------------------------------------------------------------------

function openForkDialog(version) {
    state.forkVersion = version;
    const dialog = document.getElementById("dialog-fork");
    const ctx = document.getElementById("fork-dialog-context");
    const form = document.getElementById("form-fork");
    ctx.textContent = `from version ${version.label || version.version_id} (turns ${version.start_seq}..${version.end_seq})`;
    form.elements.title.value = "";
    form.elements.fork_seq.min = String(version.start_seq);
    form.elements.fork_seq.max = String(version.end_seq);
    form.elements.fork_seq.value = String(version.start_seq);
    if (typeof dialog.showModal === "function") {
        dialog.showModal();
    } else {
        dialog.setAttribute("open", "");
    }
}

async function submitForkDialog(event) {
    const dialog = document.getElementById("dialog-fork");
    const form = document.getElementById("form-fork");
    if (event.submitter && event.submitter.value === "cancel") {
        dialog.close();
        return;
    }
    event.preventDefault();
    if (!state.forkVersion || !state.activeSessionId) {
        dialog.close();
        return;
    }
    const title = form.elements.title.value.trim();
    const forkSeq = Number(form.elements.fork_seq.value);
    if (!title || Number.isNaN(forkSeq)) return;
    const payload = {
        title,
        parent_version_id: state.forkVersion.version_id,
        fork_seq: forkSeq,
    };
    if (state.activeGroupId) payload.group_id = state.activeGroupId;
    const child = await api(
        "POST",
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/fork`,
        payload
    );
    flash(`forked "${child.title}"`);
    dialog.close();
    await loadSessions();
    await loadForkGraph();
}

// ---------------------------------------------------------------------------
// Snapshot-query dialog
// ---------------------------------------------------------------------------

function openSnapshotDialog(version) {
    state.snapshotVersion = version;
    const dialog = document.getElementById("dialog-snapshot");
    const ctx = document.getElementById("snapshot-dialog-context");
    const form = document.getElementById("form-snapshot");
    const response = document.getElementById("snapshot-response");
    ctx.textContent = `against version ${version.label || version.version_id} (turns ${version.start_seq}..${version.end_seq})`;
    form.elements.query.value = "";
    response.hidden = true;
    response.textContent = "";
    if (typeof dialog.showModal === "function") {
        dialog.showModal();
    } else {
        dialog.setAttribute("open", "");
    }
}

async function submitSnapshotDialog(event) {
    const dialog = document.getElementById("dialog-snapshot");
    const form = document.getElementById("form-snapshot");
    const responseEl = document.getElementById("snapshot-response");
    if (event.submitter && event.submitter.value === "cancel") {
        dialog.close();
        return;
    }
    event.preventDefault();
    if (!state.snapshotVersion || !state.activeSessionId) {
        dialog.close();
        return;
    }
    const query = form.elements.query.value.trim();
    if (!query) return;
    const row = await api(
        "POST",
        `/api/sessions/${encodeURIComponent(state.activeSessionId)}/snapshot-query`,
        {
            target_version_id: state.snapshotVersion.version_id,
            query,
        }
    );
    responseEl.hidden = false;
    responseEl.textContent = row.response || "(no response)";
    flash("snapshot query answered");
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
    document
        .getElementById("button-cancel-range")
        .addEventListener("click", cancelRange);
    document
        .getElementById("form-fork")
        .addEventListener("submit", submitForkDialog);
    document
        .getElementById("form-snapshot")
        .addEventListener("submit", submitSnapshotDialog);
    await loadGroups();
});
