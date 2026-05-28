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
const LAST_SESSION_KEY = "atelier:last-session-id";
const DAG_PANE_WIDTH_KEY = "atelier:dag-pane-width";
const DAG_PANE_WIDTH_MIN = 220;
const DAG_PANE_WIDTH_DEFAULT = 320;

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
     *
     * Stage J+: when the operator hops between branches we *merge* the
     * latest server snapshot into ``mergedGraph`` instead of overwriting it
     * so the DAG stays sticky -- jumping to the parent should not erase
     * the children we just rendered.
     */
    forkGraph: null,
    mergedGraph: { nodes: new Map(), edges: new Map() },
    pendingBranchSeq: null,
    pendingMemoEdge: null,
    /** Map of "<parent>:<child>" -> memo string, mirrored to localStorage. */
    edgeMemos: loadEdgeMemos(),
    /**
     * Stage L: groups indexed by id (UUID -> {group_id, name, description, color}).
     * Refreshed from ``/api/groups`` whenever the sidebar reloads.
     */
    groups: new Map(),
    /** Stage L: editing context for #group-edit dialog ("create" | UUID). */
    editingGroupId: null,
    /** Stage L: session id whose context menu is open ("" if hidden). */
    contextMenuSessionId: null,
    /**
     * Stage L: active Curator scope for the open panel.
     * ``null`` when the panel is closed.
     */
    curatorScope: null,
    /** AbortController for the in-flight curator stream, ``null`` when idle. */
    curatorStream: null,
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
    refreshMemoryChip().catch(() => {});

    // Reload survival: keep both the URL and localStorage in sync. The URL
    // is the source of truth; localStorage is the fallback when the
    // operator reloads from a stale URL like ``/`` without ``?session=``.
    // We always replaceState (so the URL matches the active session even
    // when ``pushHistory`` is false), and pushState only when the caller
    // wants a new history entry (e.g. clicking a DAG node).
    const url = new URL(window.location.href);
    if (url.searchParams.get("session") !== session.session_id) {
        url.searchParams.set("session", session.session_id);
        const target = url.toString();
        if (pushHistory) {
            window.history.pushState(
                { sessionId: session.session_id },
                "",
                target,
            );
        } else {
            window.history.replaceState(
                { sessionId: session.session_id },
                "",
                target,
            );
        }
    }
    saveLastSessionId(session.session_id);

    chatLog().innerHTML = "";
    state.streamingAssistantEl = null;
    state.streamingAssistantText = null;

    // Hydrate previous turns (if the session has any) before live-tailing.
    // The highest seq we render here becomes the live-tail starting point so
    // SSE replay does not double-render history (which previously caused
    // user/assistant grouping after a fork rather than interleaving).
    let lastSeenSeq = -1;
    try {
        lastSeenSeq = await hydrateSessionTurns(session.session_id);
    } catch (err) {
        // Hydration failure shouldn't break the chat; we just lose the
        // historical messages and proceed to live-tail from seq=0.
    }
    attachEventStream(session.session_id, lastSeenSeq + 1);
    await renderBreadcrumb(session);
    await refreshForkGraph();
}

async function hydrateSessionTurns(sessionId) {
    // Use the SSE replay endpoint with follow=false to enumerate past
    // events. We render user "turn" events and assistant "agent_turn"
    // events in seq order so the chat log matches the timeline. Returns
    // the highest seq observed (-1 if none) so the caller can resume
    // live-tail without re-emitting history.
    const resp = await fetch(
        `/api/sessions/${sessionId}/events?follow=false&from_seq=0`,
    );
    if (!resp.ok) {
        return -1;
    }
    const text = await resp.text();
    // Each SSE record is two lines (event: <kind>\n\ndata: <json>\n\n).
    // We're not following; the body is finite, so a quick line parse works.
    const lines = text.split("\n");
    let currentEvent = null;
    let lastSeq = -1;
    for (const line of lines) {
        if (line.startsWith("event:")) {
            currentEvent = line.slice(6).trim();
        } else if (line.startsWith("data:") && currentEvent) {
            const payload = JSON.parse(line.slice(5).trim());
            if (currentEvent === "turn" || currentEvent === "agent_turn") {
                renderHistoricalTurn(currentEvent, payload);
            }
            if (typeof payload.seq === "number" && payload.seq > lastSeq) {
                lastSeq = payload.seq;
            }
            currentEvent = null;
        }
    }
    return lastSeq;
}

function renderHistoricalTurn(kind, event) {
    const payload = event.payload || {};
    let role;
    if (kind === "agent_turn") {
        role = "assistant";
    } else if (payload.role === "user") {
        role = "user";
    } else {
        role = "assistant";
    }
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
    hideMemoryChip();
    setSessionLabel(null);
    lockBackendPicker(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("session");
    window.history.pushState({ sessionId: null }, "", url.toString());
    saveLastSessionId(null);
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
            // The id we tried is stale (deleted / wrong server); make
            // sure we don't keep recovering to it on the next reload.
            saveLastSessionId(null);
            const url = new URL(window.location.href);
            if (url.searchParams.get("session") === sessionId) {
                url.searchParams.delete("session");
                window.history.replaceState(
                    { sessionId: null },
                    "",
                    url.toString(),
                );
            }
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

function attachEventStream(sessionId, fromSeq = 0) {
    const start = Number.isFinite(fromSeq) && fromSeq >= 0 ? fromSeq : 0;
    const url = `/api/sessions/${sessionId}/events?follow=true&from_seq=${start}`;
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
    // Defensive: if hydrate already rendered this assistant turn, skip the
    // SSE replay so we never double-render. The `from_seq=lastSeq+1` knob
    // in attachEventStream should make this impossible, but the guard
    // protects against future regressions in either direction.
    if (
        event.seq !== undefined &&
        event.seq !== null &&
        chatLog().querySelector(`[data-seq="${event.seq}"]`)
    ) {
        return;
    }
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
        cur.title = "Double-click to rename";
        cur.addEventListener("dblclick", () =>
            renamePromptForSession(session.session_id, session.title || ""),
        );
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
            cur.title = "Double-click to rename";
            cur.addEventListener("dblclick", () =>
                renamePromptForSession(s.session_id, s.title || ""),
            );
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

    // Make sure ``state.groups`` is loaded before we render: on a hard
    // reload the boot path fires ``refreshGroups()`` and ``navigateToSession()``
    // concurrently, so without this guard the first ``renderDag`` paints
    // before groups arrive and the rect outline silently falls back to the
    // default border colour.
    if (state.groups.size === 0) {
        try {
            await refreshGroupsOnly();
        } catch (err) {
            // Best-effort: if /api/groups is unreachable, paint without colours.
        }
    }

    // Always re-seed mergedGraph from the workspace-wide session list so
    // hard-reloads do not lose roots / children that were not part of the
    // current session's ancestry. ``mergedGraph`` was an in-memory Map,
    // so before this fix only the active session's chain survived a
    // refresh and every other root vanished from the DAG.
    await hydrateAllSessionsIntoMergedGraph();

    if (state.sessionId) {
        // Layer the active session's authoritative fork-graph (group or
        // ancestry) on top: this is the freshest data per child / version,
        // and ``mergeForkGraph`` overwrites entries by id so we never
        // shadow newer titles / versions with the cached list payload.
        const session = state.sessionsCache.get(state.sessionId);
        const groupId = session ? session.group_id : null;
        let snapshot;
        if (!groupId) {
            snapshot = await buildSyntheticGraph(state.sessionId);
        } else {
            try {
                snapshot = await api("GET", `/api/groups/${groupId}/fork-graph`);
            } catch (err) {
                snapshot = await buildSyntheticGraph(state.sessionId);
            }
        }
        state.forkGraph = snapshot;
        mergeForkGraph(snapshot);
    } else {
        // No active session yet (e.g. hard reload without ?session=...):
        // we still want to show the workspace overview, so don't clear
        // ``forkGraph`` here.
        state.forkGraph = null;
    }

    const merged = mergedGraphAsPayload();
    if (!merged.nodes.length) {
        dagEmpty.removeAttribute("hidden");
        svg.setAttribute("hidden", "");
        return;
    }
    // Even a solo (single-node, no edges) session must render: the
    // operator wants to see its group colour and right-click it to assign
    // / reassign / open Curator. The previous "<=1 -> hide" rule made
    // freshly-reloaded single-session workspaces appear empty.
    dagEmpty.setAttribute("hidden", "");
    svg.removeAttribute("hidden");
    renderDag(merged);
}

async function hydrateAllSessionsIntoMergedGraph() {
    // Pulls the full session list once and seeds ``state.sessionsCache``
    // + ``state.mergedGraph`` so the DAG covers every root + child even
    // immediately after a hard reload (when both maps start empty).
    let listing;
    try {
        listing = await api("GET", "/api/sessions");
    } catch (err) {
        return;
    }
    if (!Array.isArray(listing)) {
        return;
    }
    for (const s of listing) {
        state.sessionsCache.set(s.session_id, s);
        const existing = state.mergedGraph.nodes.get(s.session_id);
        // Don't clobber a node we already merged from a fork-graph
        // payload, but make sure missing nodes appear with their
        // group / parent metadata.
        if (!existing) {
            state.mergedGraph.nodes.set(s.session_id, {
                session_id: s.session_id,
                title: s.title,
                status: s.status,
                parent_session_id: s.parent_session_id,
                parent_version_id: s.parent_version_id,
                fork_seq: s.fork_seq,
                versions: [],
                group_id: s.group_id || null,
            });
        } else if (existing.group_id == null && s.group_id) {
            // group can be (re)assigned after the first merge; keep it
            // current so the colour follows the latest server state.
            state.mergedGraph.nodes.set(s.session_id, {
                ...existing,
                group_id: s.group_id,
            });
        }
        if (s.parent_session_id) {
            const key = `${s.parent_session_id}:${s.session_id}`;
            if (!state.mergedGraph.edges.has(key)) {
                state.mergedGraph.edges.set(key, {
                    parent_session_id: s.parent_session_id,
                    child_session_id: s.session_id,
                    via_version_id: s.parent_version_id,
                    fork_seq: s.fork_seq,
                });
            }
        }
    }
}

function mergeForkGraph(graph) {
    if (!graph) return;
    for (const n of graph.nodes || []) {
        // Latest snapshot wins per session id (title / status / versions
        // are the freshest). Parent pointers are immutable, so the merge
        // is a simple overwrite.
        state.mergedGraph.nodes.set(n.session_id, n);
    }
    for (const e of graph.edges || []) {
        const key = `${e.parent_session_id}:${e.child_session_id}`;
        state.mergedGraph.edges.set(key, e);
    }
}

function mergedGraphAsPayload() {
    return {
        nodes: Array.from(state.mergedGraph.nodes.values()),
        edges: Array.from(state.mergedGraph.edges.values()),
    };
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
        const isRoot = !n.parent_session_id;
        const groupId = n.group_id || null;
        if (groupId) {
            g.classList.add("has-group");
            g.dataset.groupId = groupId;
        }
        if (isRoot) {
            g.dataset.root = "true";
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
        if (groupId) {
            const grp = state.groups.get(groupId);
            if (grp && grp.color) {
                // ``setAttribute("stroke", ...)`` is a presentation
                // attribute and loses to the ``.dag-node rect { stroke: ... }``
                // rule in chat.css. Inline styles win over rule-based
                // selectors, so colour the outline via ``style.stroke``.
                rect.style.stroke = grp.color;
                rect.style.strokeWidth = "2.5px";
            }
        }
        g.appendChild(rect);

        const text = document.createElementNS(
            "http://www.w3.org/2000/svg",
            "text",
        );
        text.setAttribute("x", String(pos.x + 10));
        text.setAttribute("y", String(pos.y + nodeHeight / 2 + 4));
        const fullTitle = n.title || shortId(n.session_id);
        const title = fullTitle.slice(0, 22);
        text.textContent = title;
        g.appendChild(text);

        const fullTitleNode = document.createElementNS(
            "http://www.w3.org/2000/svg",
            "title",
        );
        const groupHint = groupId
            ? ` -- group: ${state.groups.get(groupId)?.name || groupId}`
            : "";
        fullTitleNode.textContent =
            `${fullTitle}${groupHint} (double-click to rename, right-click for actions)`;
        g.appendChild(fullTitleNode);

        g.addEventListener("click", () => navigateToSession(n.session_id));
        g.addEventListener("dblclick", (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            renamePromptForSession(n.session_id, fullTitle);
        });
        g.addEventListener("contextmenu", (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            openNodeContextMenu(n, ev.clientX, ev.clientY);
        });
        svg.appendChild(g);
    }
}

async function renamePromptForSession(sessionId, currentTitle) {
    const next = window.prompt("Rename node", currentTitle || "");
    if (next === null) return;
    const trimmed = next.trim();
    if (!trimmed || trimmed === currentTitle) return;
    if (trimmed.length > 200) {
        flash("Title too long (max 200 chars)");
        return;
    }
    try {
        const updated = await api("PATCH", `/api/sessions/${sessionId}`, {
            title: trimmed,
        });
        state.sessionsCache.set(updated.session_id, updated);
        const node = state.mergedGraph.nodes.get(sessionId);
        if (node) {
            state.mergedGraph.nodes.set(sessionId, { ...node, title: updated.title });
        }
        if (state.sessionId === sessionId) {
            await renderBreadcrumb(updated);
        }
        renderDag(mergedGraphAsPayload());
        flash("Renamed");
    } catch (err) {
        flash(`Rename failed: ${err.message || err}`);
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

function saveLastSessionId(sessionId) {
    try {
        if (sessionId) {
            window.localStorage.setItem(LAST_SESSION_KEY, sessionId);
        } else {
            window.localStorage.removeItem(LAST_SESSION_KEY);
        }
    } catch (err) {
        // Quota / private mode -- non-critical; URL is the primary cursor.
    }
}

function loadLastSessionId() {
    try {
        const raw = window.localStorage.getItem(LAST_SESSION_KEY);
        return raw && typeof raw === "string" ? raw : null;
    } catch (err) {
        return null;
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
// Stage J+: DAG pane resizer (drag the divider between chat and DAG)
// ---------------------------------------------------------------------------

function initDagResizer() {
    const resizer = document.getElementById("dag-resizer");
    const layout = document.querySelector(".chat-layout");
    if (!resizer || !layout) return;

    const stored = Number(localStorage.getItem(DAG_PANE_WIDTH_KEY));
    if (Number.isFinite(stored) && stored >= DAG_PANE_WIDTH_MIN) {
        applyDagPaneWidth(stored);
    }

    let dragging = false;

    function onMove(ev) {
        if (!dragging) return;
        ev.preventDefault();
        const rect = layout.getBoundingClientRect();
        // Pane width = right edge of layout minus pointer X.
        let next = rect.right - ev.clientX;
        const max = Math.max(DAG_PANE_WIDTH_MIN, rect.width - 320);
        if (next < DAG_PANE_WIDTH_MIN) next = DAG_PANE_WIDTH_MIN;
        if (next > max) next = max;
        applyDagPaneWidth(next);
    }

    function onUp() {
        if (!dragging) return;
        dragging = false;
        resizer.classList.remove("is-dragging");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        const current = currentDagPaneWidth();
        if (current >= DAG_PANE_WIDTH_MIN) {
            localStorage.setItem(DAG_PANE_WIDTH_KEY, String(Math.round(current)));
        }
        if (state.forkGraph || state.mergedGraph.nodes.size > 0) {
            renderDag(mergedGraphAsPayload());
        }
    }

    resizer.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        dragging = true;
        resizer.classList.add("is-dragging");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
    });

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);

    // Keyboard handle: arrow keys nudge the pane in 24px steps.
    resizer.addEventListener("keydown", (ev) => {
        const step = ev.shiftKey ? 80 : 24;
        if (ev.key === "ArrowLeft") {
            applyDagPaneWidth(currentDagPaneWidth() + step);
            ev.preventDefault();
        } else if (ev.key === "ArrowRight") {
            applyDagPaneWidth(Math.max(DAG_PANE_WIDTH_MIN, currentDagPaneWidth() - step));
            ev.preventDefault();
        } else {
            return;
        }
        localStorage.setItem(
            DAG_PANE_WIDTH_KEY,
            String(Math.round(currentDagPaneWidth())),
        );
        if (state.forkGraph || state.mergedGraph.nodes.size > 0) {
            renderDag(mergedGraphAsPayload());
        }
    });
}

function currentDagPaneWidth() {
    const layout = document.querySelector(".chat-layout");
    if (!layout) return DAG_PANE_WIDTH_DEFAULT;
    const styled = getComputedStyle(layout).getPropertyValue("--dag-pane-width");
    const px = parseFloat(styled);
    if (Number.isFinite(px) && px > 0) return px;
    return DAG_PANE_WIDTH_DEFAULT;
}

function applyDagPaneWidth(px) {
    const layout = document.querySelector(".chat-layout");
    if (!layout) return;
    layout.style.setProperty("--dag-pane-width", `${Math.round(px)}px`);
}

// ---------------------------------------------------------------------------
// Stage L: groups sidebar + node context menu + group edit dialog
// ---------------------------------------------------------------------------

const DEFAULT_GROUP_COLOR = "#3B82F6";

async function refreshGroupsOnly() {
    // Loads ``/api/groups`` into ``state.groups`` *without* re-rendering
    // the DAG. ``refreshForkGraph`` calls this when groups are missing on
    // first paint (hard-reload race) so the subsequent ``renderDag`` has
    // the colour map it needs.
    let groups;
    try {
        groups = await api("GET", "/api/groups");
    } catch (err) {
        groups = [];
    }
    state.groups.clear();
    for (const g of groups) {
        state.groups.set(g.group_id, g);
    }
    renderGroupList();
}

async function refreshGroups() {
    await refreshGroupsOnly();
    if (state.forkGraph || state.mergedGraph.nodes.size > 0) {
        renderDag(mergedGraphAsPayload());
    }
}

function renderGroupList() {
    const list = document.getElementById("group-list");
    if (!list) return;
    list.innerHTML = "";
    for (const g of state.groups.values()) {
        const li = document.createElement("li");
        li.className = "group-list-item";
        li.dataset.groupId = g.group_id;
        li.dataset.testid = "group-list-item";

        const swatch = document.createElement("span");
        swatch.className = "group-swatch";
        swatch.style.background = g.color;
        li.appendChild(swatch);

        const name = document.createElement("span");
        name.className = "group-name";
        name.textContent = g.name;
        name.title = g.description || g.name;
        li.appendChild(name);

        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "group-edit-button";
        editBtn.textContent = "Edit";
        editBtn.dataset.testid = "group-edit-open";
        editBtn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            openGroupDialog(g.group_id);
        });
        li.appendChild(editBtn);

        list.appendChild(li);
    }
}

function openGroupDialog(groupId) {
    const dlg = document.getElementById("group-edit");
    if (!dlg) return;
    state.editingGroupId = groupId || "create";
    const titleEl = document.getElementById("group-edit-title");
    const nameEl = document.getElementById("group-edit-name");
    const descEl = document.getElementById("group-edit-description");
    const colorEl = document.getElementById("group-edit-color");
    const deleteBtn = document.getElementById("group-edit-delete");
    if (groupId && state.groups.has(groupId)) {
        const g = state.groups.get(groupId);
        titleEl.textContent = `Edit group: ${g.name}`;
        nameEl.value = g.name;
        descEl.value = g.description || "";
        colorEl.value = g.color || DEFAULT_GROUP_COLOR;
        deleteBtn.removeAttribute("hidden");
    } else {
        titleEl.textContent = "New group";
        nameEl.value = "";
        descEl.value = "";
        colorEl.value = DEFAULT_GROUP_COLOR;
        deleteBtn.setAttribute("hidden", "");
    }
    if (typeof dlg.showModal === "function") {
        dlg.showModal();
    } else {
        dlg.setAttribute("open", "");
    }
}

async function saveGroupFromDialog() {
    const nameEl = document.getElementById("group-edit-name");
    const descEl = document.getElementById("group-edit-description");
    const colorEl = document.getElementById("group-edit-color");
    const name = (nameEl?.value || "").trim();
    if (!name) {
        flash("Group name is required");
        return;
    }
    const description = (descEl?.value || "").trim() || null;
    const color = colorEl?.value || DEFAULT_GROUP_COLOR;
    try {
        if (state.editingGroupId === "create") {
            await api("POST", "/api/groups", { name, description, color });
            flash("Group created");
        } else if (state.editingGroupId) {
            await api("PATCH", `/api/groups/${state.editingGroupId}`, {
                name,
                description,
                color,
            });
            flash("Group updated");
        }
        closeDialog(document.getElementById("group-edit"));
        await refreshGroups();
    } catch (err) {
        flash(`Group save failed: ${err.message || err}`);
    }
}

async function deleteGroupFromDialog() {
    if (!state.editingGroupId || state.editingGroupId === "create") {
        return;
    }
    const g = state.groups.get(state.editingGroupId);
    const label = g ? g.name : state.editingGroupId;
    const ok = window.confirm(
        `Delete group "${label}"? Sessions will be detached but kept.`,
    );
    if (!ok) return;
    try {
        await api("DELETE", `/api/groups/${state.editingGroupId}`);
        flash("Group deleted");
        closeDialog(document.getElementById("group-edit"));
        await refreshGroups();
    } catch (err) {
        flash(`Delete failed: ${err.message || err}`);
    }
}

function openNodeContextMenu(node, clientX, clientY) {
    const menu = document.getElementById("node-context-menu");
    if (!menu) return;
    menu.innerHTML = "";
    state.contextMenuSessionId = node.session_id;
    const isRoot = !node.parent_session_id;

    const renameItem = document.createElement("li");
    renameItem.textContent = "Rename...";
    renameItem.dataset.action = "rename";
    renameItem.addEventListener("click", () => {
        closeNodeContextMenu();
        renamePromptForSession(node.session_id, node.title || "");
    });
    menu.appendChild(renameItem);

    const curatorDivider = document.createElement("li");
    curatorDivider.className = "is-divider";
    curatorDivider.textContent = "Curator";
    menu.appendChild(curatorDivider);

    const askSession = document.createElement("li");
    askSession.dataset.testid = "node-context-curator-session";
    askSession.textContent = "Ask Curator about this session";
    askSession.addEventListener("click", () => {
        closeNodeContextMenu();
        openCuratorPanel({
            scopeKind: "session",
            scopeId: node.session_id,
            summary: `session ${node.title || node.session_id}`,
        });
    });
    menu.appendChild(askSession);

    if (node.group_id) {
        const groupName = state.groups.get(node.group_id)?.name || "group";
        const askGroup = document.createElement("li");
        askGroup.dataset.testid = "node-context-curator-group";
        askGroup.textContent = `Ask Curator about group "${groupName}"`;
        askGroup.addEventListener("click", () => {
            closeNodeContextMenu();
            openCuratorPanel({
                scopeKind: "group",
                scopeId: node.group_id,
                summary: `group "${groupName}"`,
            });
        });
        menu.appendChild(askGroup);
    }

    if (isRoot) {
        const divider = document.createElement("li");
        divider.className = "is-divider";
        divider.textContent = "Group";
        menu.appendChild(divider);

        if (state.groups.size === 0) {
            const empty = document.createElement("li");
            empty.className = "is-disabled";
            empty.textContent = "(no groups -- create one first)";
            menu.appendChild(empty);
        } else {
            for (const g of state.groups.values()) {
                const item = document.createElement("li");
                item.dataset.testid = "node-context-group-option";
                const swatch = document.createElement("span");
                swatch.className = "group-swatch";
                swatch.style.background = g.color;
                item.appendChild(swatch);
                const label = document.createElement("span");
                const isCurrent = node.group_id === g.group_id;
                label.textContent = `${isCurrent ? "[current] " : ""}${g.name}`;
                item.appendChild(label);
                item.addEventListener("click", () => {
                    closeNodeContextMenu();
                    assignSessionToGroup(node.session_id, g.group_id).catch(() => {});
                });
                menu.appendChild(item);
            }
        }

        if (node.group_id) {
            const detach = document.createElement("li");
            detach.dataset.testid = "node-context-detach";
            detach.textContent = "Remove from group";
            detach.addEventListener("click", () => {
                closeNodeContextMenu();
                assignSessionToGroup(node.session_id, null).catch(() => {});
            });
            menu.appendChild(detach);
        }
    } else {
        const divider = document.createElement("li");
        divider.className = "is-divider";
        divider.textContent = "Group";
        menu.appendChild(divider);
        const note = document.createElement("li");
        note.className = "is-disabled";
        note.textContent = "(forks inherit the root's group)";
        menu.appendChild(note);
    }

    menu.style.left = `${clientX}px`;
    menu.style.top = `${clientY}px`;
    menu.removeAttribute("hidden");
}

function closeNodeContextMenu() {
    const menu = document.getElementById("node-context-menu");
    if (menu) {
        menu.setAttribute("hidden", "");
    }
    state.contextMenuSessionId = null;
}

async function assignSessionToGroup(sessionId, groupId) {
    try {
        await api("PUT", `/api/sessions/${sessionId}/group`, {
            group_id: groupId,
        });
        flash(groupId ? "Added to group" : "Removed from group");
        await refreshForkGraph();
    } catch (err) {
        flash(`Assign failed: ${err.message || err}`);
    }
}

// ---------------------------------------------------------------------------
// Stage L: Curator panel (isolated scope-bound Q&A)
// ---------------------------------------------------------------------------

function curatorDialog() {
    return document.getElementById("curator-panel");
}

function openCuratorPanel({ scopeKind, scopeId, summary }) {
    const dialog = curatorDialog();
    if (!dialog) return;
    state.curatorScope = { scopeKind, scopeId, summary };
    const summaryEl = document.getElementById("curator-scope-summary");
    if (summaryEl) {
        summaryEl.textContent = `${scopeKind} -- ${summary}`;
    }
    const answerSection = document.querySelector(
        "[data-testid='curator-answer-section']"
    );
    if (answerSection) {
        answerSection.setAttribute("hidden", "");
    }
    const answerEl = document.getElementById("curator-answer");
    if (answerEl) {
        answerEl.textContent = "";
        answerEl.classList.remove("streaming");
    }
    const statusEl = document.getElementById("curator-answer-status");
    if (statusEl) {
        statusEl.textContent = "";
    }
    const askBtn = document.getElementById("curator-ask");
    if (askBtn) {
        askBtn.disabled = false;
        askBtn.textContent = "Ask";
    }
    if (typeof dialog.showModal === "function" && !dialog.open) {
        dialog.showModal();
    } else {
        dialog.setAttribute("open", "");
    }
    document.getElementById("curator-question")?.focus();
}

function closeCuratorPanel() {
    if (state.curatorStream) {
        state.curatorStream.abort();
        state.curatorStream = null;
    }
    state.curatorScope = null;
    closeDialog(curatorDialog());
}

async function runCuratorQuery() {
    const scope = state.curatorScope;
    if (!scope) return;
    const questionEl = document.getElementById("curator-question");
    const question = (questionEl?.value || "").trim();
    if (!question) {
        flash("Question cannot be empty");
        questionEl?.focus();
        return;
    }
    const modeEl = document.querySelector(
        "input[name='curator-mode']:checked"
    );
    const contextMode = modeEl?.value === "distill" ? "distill" : "raw";

    const askBtn = document.getElementById("curator-ask");
    if (askBtn) {
        askBtn.disabled = true;
        askBtn.textContent = "Asking...";
    }
    const answerSection = document.querySelector(
        "[data-testid='curator-answer-section']"
    );
    answerSection?.removeAttribute("hidden");
    const answerEl = document.getElementById("curator-answer");
    if (answerEl) {
        answerEl.textContent = "";
        answerEl.classList.add("streaming");
    }
    const statusEl = document.getElementById("curator-answer-status");
    if (statusEl) {
        statusEl.textContent = "Streaming...";
    }

    const controller = new AbortController();
    state.curatorStream = controller;
    try {
        const resp = await fetch("/api/curator/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                scope_kind: scope.scopeKind,
                scope_id: scope.scopeId,
                context_mode: contextMode,
                question,
            }),
            signal: controller.signal,
        });
        if (!resp.ok) {
            const text = await resp.text();
            throw new Error(`${resp.status} ${text}`);
        }
        await consumeCuratorSse(resp, answerEl, statusEl);
    } catch (err) {
        if (controller.signal.aborted) {
            // closing the panel cancelled the stream; keep UI quiet.
            return;
        }
        if (statusEl) {
            statusEl.textContent = `Failed: ${err.message || err}`;
        }
        if (answerEl) {
            answerEl.classList.remove("streaming");
        }
        flash(`Curator failed: ${err.message || err}`);
    } finally {
        if (state.curatorStream === controller) {
            state.curatorStream = null;
        }
        if (askBtn) {
            askBtn.disabled = false;
            askBtn.textContent = "Ask again";
        }
    }
}

async function consumeCuratorSse(resp, answerEl, statusEl) {
    if (!resp.body) {
        throw new Error("response has no body");
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let endTurnSeen = false;
    while (true) {
        const { value, done } = await reader.read();
        if (value) {
            buffer += decoder.decode(value, { stream: true });
            let idx = buffer.indexOf("\n\n");
            while (idx !== -1) {
                const frame = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 2);
                const handled = handleCuratorFrame(frame, answerEl, statusEl);
                if (handled === "end_turn") {
                    endTurnSeen = true;
                }
                idx = buffer.indexOf("\n\n");
            }
        }
        if (done) break;
    }
    if (answerEl) {
        answerEl.classList.remove("streaming");
    }
    if (statusEl) {
        statusEl.textContent = endTurnSeen ? "Done" : "Disconnected";
    }
}

function handleCuratorFrame(frame, answerEl, statusEl) {
    let eventType = "message";
    let dataLines = [];
    for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) {
            eventType = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trim());
        }
    }
    const raw = dataLines.join("\n");
    let payload = {};
    if (raw) {
        try {
            payload = JSON.parse(raw);
        } catch (err) {
            payload = { text: raw };
        }
    }
    if (eventType === "text_delta") {
        const text = typeof payload.text === "string" ? payload.text : "";
        if (text && answerEl) {
            answerEl.textContent += text;
        }
    } else if (eventType === "error") {
        if (statusEl) {
            statusEl.textContent = `Error: ${payload.error || "unknown"}`;
        }
    } else if (eventType === "end_turn") {
        return "end_turn";
    }
    return eventType;
}

// ---------------------------------------------------------------------------
// Memory chip (queued context for the next turn)
// ---------------------------------------------------------------------------

function renderMemoryChip(block) {
    const chip = document.getElementById("memory-chip");
    const label = document.querySelector("[data-testid='memory-chip-label']");
    if (!chip || !label) {
        return;
    }
    const collapsed = (block || "").replace(/\s+/g, " ").trim();
    const preview = collapsed.length > 80 ? `${collapsed.slice(0, 80)}…` : collapsed;
    label.textContent = `Memory queued: ${preview || "(empty)"}`;
    chip.removeAttribute("hidden");
}

function hideMemoryChip() {
    const chip = document.getElementById("memory-chip");
    if (chip) {
        chip.setAttribute("hidden", "");
    }
}

async function refreshMemoryChip() {
    if (!state.sessionId) {
        hideMemoryChip();
        return;
    }
    try {
        const resp = await api("GET", `/api/memory/adopt/${state.sessionId}`);
        if (resp && resp.pending && resp.memory_block) {
            renderMemoryChip(resp.memory_block);
        } else {
            hideMemoryChip();
        }
    } catch (err) {
        hideMemoryChip();
    }
}

async function clearMemoryChip() {
    if (!state.sessionId) {
        hideMemoryChip();
        return;
    }
    try {
        await api("DELETE", `/api/memory/adopt/${state.sessionId}`);
    } catch (err) {
        // Even if the server disagrees, hide the chip locally.
    }
    hideMemoryChip();
    flash("Memory chip cleared");
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
        .addEventListener("click", () => {
            refreshGroups().catch(() => {});
            refreshForkGraph().catch(() => {});
        });
    document
        .getElementById("button-group-new")
        ?.addEventListener("click", () => openGroupDialog(null));
    document
        .getElementById("group-edit-save")
        ?.addEventListener("click", () => {
            saveGroupFromDialog().catch(() => {});
        });
    document
        .getElementById("group-edit-cancel")
        ?.addEventListener("click", () => {
            closeDialog(document.getElementById("group-edit"));
        });
    document
        .getElementById("group-edit-delete")
        ?.addEventListener("click", () => {
            deleteGroupFromDialog().catch(() => {});
        });
    document.addEventListener("click", (ev) => {
        const menu = document.getElementById("node-context-menu");
        if (!menu || menu.hasAttribute("hidden")) return;
        if (!menu.contains(ev.target)) {
            closeNodeContextMenu();
        }
    });
    document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") {
            closeNodeContextMenu();
        }
    });
    document
        .getElementById("memory-chip-clear")
        ?.addEventListener("click", () => {
            clearMemoryChip().catch(() => {});
        });
    document
        .getElementById("curator-ask")
        ?.addEventListener("click", () => {
            runCuratorQuery().catch(() => {});
        });
    document
        .getElementById("curator-close")
        ?.addEventListener("click", () => {
            closeCuratorPanel();
        });
    curatorDialog()?.addEventListener("close", () => {
        if (state.curatorStream) {
            state.curatorStream.abort();
            state.curatorStream = null;
        }
        state.curatorScope = null;
    });
    const fitBtn = document.getElementById("button-dag-fit");
    if (fitBtn) {
        fitBtn.addEventListener("click", () => {
            if (state.forkGraph || state.mergedGraph.nodes.size > 0) {
                renderDag(mergedGraphAsPayload());
            }
        });
    }
    initDagResizer();

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

    // Stage J/L: deep-link via ?session=<id>. Picking up a pre-existing
    // session preserves the chat after a hard refresh.
    //
    // Order matters: we must finish ``refreshGroups`` before
    // ``navigateToSession`` ends up in ``renderDag``, otherwise the first
    // paint runs without ``state.groups`` populated and the rect outline
    // falls back to the default border colour. ``refreshForkGraph`` has
    // its own defensive fallback (it awaits ``refreshGroupsOnly`` if the
    // map is empty), but boot-time sequencing keeps that fallback rare.
    // Reload survival: URL ``?session=`` is the primary cursor; if it's
    // missing (e.g. the operator opened a bare ``/``), fall back to the
    // last session id we stashed in localStorage. Either way the user
    // lands on the same session their last tab was on.
    const params = new URLSearchParams(window.location.search);
    const urlSessionId = params.get("session");
    const stickySessionId = urlSessionId || loadLastSessionId();
    (async () => {
        try {
            await refreshGroups();
        } catch (err) {
            // best-effort: empty groups still lets the chat render
        }
        if (stickySessionId) {
            try {
                await navigateToSession(stickySessionId);
            } catch (err) {
                // Stale localStorage / bogus URL: drop to "new session" UX.
                saveLastSessionId(null);
            }
        }
    })();
}

boot();

// Exposed for unit/E2E tests; keeps the production module self-contained.
if (typeof window !== "undefined") {
    window.__atelier = {
        state,
        layoutDag,
        loadEdgeMemos,
        hydrateSessionTurns,
        attachEventStream,
        renderMemoryChip,
        hideMemoryChip,
        refreshMemoryChip,
        clearMemoryChip,
        refreshGroups,
        renderGroupList,
        openGroupDialog,
        saveGroupFromDialog,
        deleteGroupFromDialog,
        openNodeContextMenu,
        closeNodeContextMenu,
        assignSessionToGroup,
        openCuratorPanel,
        closeCuratorPanel,
        runCuratorQuery,
        handleCuratorFrame,
    };
}
