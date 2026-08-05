// autolox web app — iteration 1.
// Vanilla ESM, no framework. One page, four screens, SSE tap stream.

const $ = (id) => document.getElementById(id);
const on = (el, evt, fn) => el.addEventListener(evt, fn);

// -------------------- state --------------------

const state = {
    readers: [],
    users: [],
    setup: {
        reader_uuid: null,
        user_uuid: null,
        user_name: null,
        roster: "",
        dry_run: true,
        fresh: false,
    },
    session: null,        // {session_id, reader, user_name, transformed, already_bound_count, to_do_count, dry_run}
    live: null,           // {es, boundIndex, totalToBind, rosterState}
};

const screens = ["setup", "review", "enrol", "summary"];
// screens that have a stepnav position. connect-error is a separate
// full-page state and doesn't participate in the numbered flow.
const stepnavScreens = new Set(screens);

// -------------------- screen switching --------------------

function goTo(step) {
    const allScreens = [...screens, "connect-error"];
    for (const s of allScreens) $(`screen-${s}`).classList.toggle("active", s === step);
    $("step-title").textContent =
        step === "setup"         ? "Setup"     :
        step === "review"        ? "Review"    :
        step === "enrol"         ? "Enrolling" :
        step === "summary"       ? "Summary"   :
        step === "connect-error" ? "Offline"   :
                                    "";
    // Hide the stepnav footer when we're on a non-flow screen (e.g. the
    // connect-error page) so it doesn't imply we're inside the flow.
    const footer = document.querySelector(".stepnav");
    if (footer) footer.style.display = stepnavScreens.has(step) ? "" : "none";
    for (const el of document.querySelectorAll(".stepnav-dot")) {
        const s = el.dataset.step;
        el.classList.remove("active", "done");
        if (s === step) el.classList.add("active");
        else if (screens.indexOf(s) < screens.indexOf(step)) el.classList.add("done");
    }
}

// -------------------- API helpers --------------------

async function apiGet(path) {
    const r = await fetch(path);
    if (!r.ok) throw await apiError(r);
    return r.json();
}

async function apiPost(path, body) {
    const r = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: body === undefined ? null : JSON.stringify(body),
    });
    if (!r.ok) throw await apiError(r);
    return r.json();
}

async function apiError(r) {
    let msg = `HTTP ${r.status}`;
    try {
        const body = await r.json();
        msg = body.detail || body.error || msg;
    } catch (_) { /* ignore */ }
    return new Error(msg);
}

// -------------------- setup screen --------------------

async function loadDiscovery() {
    try {
        const [readers, users] = await Promise.all([
            apiGet("/api/readers"),
            apiGet("/api/users"),
        ]);
        state.readers = readers;
        state.users = users;
        renderReaders();
        renderUsers("");
        // In case we were on the connect-error screen, come back to Setup.
        if ($("screen-connect-error").classList.contains("active")) {
            goTo("setup");
        }
    } catch (e) {
        showConnectError(e.message);
    }
}

function showConnectError(msg) {
    $("connect-error-msg").textContent = msg || "Unknown error";
    goTo("connect-error");
}

on($("btn-retry-connect"), "click", () => {
    $("btn-retry-connect").disabled = true;
    $("btn-retry-connect").textContent = "Retrying…";
    loadDiscovery().finally(() => {
        $("btn-retry-connect").disabled = false;
        $("btn-retry-connect").textContent = "Retry";
    });
});

function renderReaders() {
    const sel = $("reader-select");
    sel.innerHTML = "";
    if (!state.readers.length) {
        $("reader-help").textContent = "No readers visible to this account. "
                                     + "Check the service account's group access.";
        return;
    }
    for (const r of state.readers) {
        const opt = document.createElement("option");
        opt.value = r.uuid_action;
        opt.textContent = `${r.name} — ${r.room || "no room"}`;
        sel.appendChild(opt);
    }
    $("reader-help").textContent =
        `${state.readers.length} reader(s) available. Pick the one you're standing at.`;
    state.setup.reader_uuid = state.readers[0].uuid_action;
    updateContinueButton();
}

function renderUsers(filter) {
    const sel = $("user-select");
    sel.innerHTML = "";
    const needle = filter.trim().toLowerCase();
    const matches = needle
        ? state.users.filter(u =>
              u.name.toLowerCase().includes(needle) ||
              u.uuid.toLowerCase().includes(needle))
        : state.users;
    for (const u of matches) {
        const opt = document.createElement("option");
        opt.value = u.uuid;
        opt.textContent = `${u.name}${u.is_admin ? " (admin)" : ""} — ${u.nfc_tag_count} tag(s)`;
        opt.dataset.name = u.name;
        sel.appendChild(opt);
    }
    $("user-help").textContent = matches.length
        ? "Cards will be attached to the selected user. Pick carefully."
        : `No users match "${filter}".`;
}

function updateContinueButton() {
    const btn = $("btn-review");
    const rosterOK = state.setup.roster.trim().split("\n")
                        .filter(l => l.trim()).length > 0;
    btn.disabled = !(state.setup.reader_uuid && state.setup.user_uuid && rosterOK);
}

on($("reader-select"), "change", (e) => {
    state.setup.reader_uuid = e.target.value;
    updateContinueButton();
});
on($("user-select"), "change", (e) => {
    state.setup.user_uuid = e.target.value;
    const sel = e.target.selectedOptions[0];
    state.setup.user_name = sel ? sel.dataset.name : null;
    updateContinueButton();
});
on($("user-filter"), "input", (e) => renderUsers(e.target.value));
on($("roster-input"), "input", (e) => {
    state.setup.roster = e.target.value;
    updateContinueButton();
    updateRosterCount();
});

function updateRosterCount() {
    const lines = ($("roster-input").value || "").split("\n")
                    .filter(l => l.trim());
    $("roster-count").textContent = lines.length
        ? `${lines.length} name${lines.length === 1 ? "" : "s"}`
        : "";
}

function applyRosterTransform(fn) {
    const before = $("roster-input").value;
    const lines = before.split("\n");
    const after = fn(lines).join("\n");
    if (after !== before) {
        $("roster-input").value = after;
        state.setup.roster = after;
        updateContinueButton();
        updateRosterCount();
    }
}

on($("roster-trim"), "click", () => {
    applyRosterTransform(lines => lines
        .map(l => l.trim())
        .filter(l => l.length > 0));
});

on($("roster-dedup"), "click", () => {
    applyRosterTransform(lines => {
        const seen = new Set();
        const out = [];
        for (const l of lines) {
            const key = l.trim().toLowerCase();
            if (!key) { out.push(l); continue; }  // preserve blank lines
            if (!seen.has(key)) {
                seen.add(key);
                out.push(l);
            }
        }
        return out;
    });
});

on($("roster-sort"), "click", () => {
    applyRosterTransform(lines => {
        // Sort case-insensitive by trimmed value. Blank lines drop out
        // implicitly because they'd all be equivalent — call this a
        // side benefit rather than a bug.
        return lines
            .map(l => l.trim())
            .filter(l => l.length > 0)
            .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
    });
});
on($("dry-run-toggle"), "change", (e) => {
    state.setup.dry_run = e.target.checked;
});
on($("fresh-toggle"), "change", (e) => {
    state.setup.fresh = e.target.checked;
});

on($("btn-review"), "click", async () => {
    const btn = $("btn-review");
    btn.disabled = true;
    btn.textContent = "Preparing…";
    try {
        const rosterLines = state.setup.roster.split("\n")
                                .map(l => l.trim()).filter(l => l);
        const res = await apiPost("/api/session", {
            reader_uuid: state.setup.reader_uuid,
            user_uuid: state.setup.user_uuid,
            roster: rosterLines,
            dry_run: state.setup.dry_run,
            fresh: state.setup.fresh,
        });
        state.session = res;
        renderReview();
        goTo("review");
    } catch (e) {
        alert(`Could not prepare session:\n${e.message}`);
    } finally {
        btn.disabled = false;
        btn.textContent = "Continue → Review";
    }
});

// -------------------- review screen --------------------

function renderReview() {
    const s = state.session;
    $("review-reader").textContent = `${s.reader.name} (${s.reader.room || "—"})`;
    $("review-user").textContent   = s.user_name;
    $("review-mode").textContent   = s.dry_run ? "DRY RUN — no writes" : "LIVE";
    $("review-count").textContent  = `${s.transformed.length} name(s)`;
    $("review-preload").textContent = s.already_bound_count > 0
        ? `${s.already_bound_count} tag(s) — will be skipped`
        : "no prior bindings";

    const tbody = $("transform-table").querySelector("tbody");
    tbody.innerHTML = "";
    for (const row of s.transformed) {
        const tr = document.createElement("tr");
        if (row.warnings.length) tr.classList.add("warn");
        tr.innerHTML = `
            <td class="mark">${row.warnings.length ? "!" : ""}</td>
            <td>${escape(row.roster)}</td>
            <td class="arrow">→</td>
            <td>${escape(row.loxone)}</td>`;
        tbody.appendChild(tr);
        if (row.warnings.length) {
            const wtr = document.createElement("tr");
            wtr.classList.add("warn");
            const td = document.createElement("td");
            td.colSpan = 4;
            td.classList.add("warnings");
            td.textContent = "↳ " + row.warnings.join("; ");
            wtr.appendChild(td);
            tbody.appendChild(wtr);
        }
    }
}

on($("btn-back-to-setup"), "click", async () => {
    // Cancel the just-created session server-side, return to setup
    if (state.session) {
        try {
            await apiPost(`/api/session/${state.session.session_id}/stop`);
        } catch (_) { /* server may already have closed */ }
        state.session = null;
    }
    goTo("setup");
});

on($("btn-arm"), "click", async () => {
    const btn = $("btn-arm");
    btn.disabled = true;
    btn.textContent = "Arming…";
    try {
        await apiPost(`/api/session/${state.session.session_id}/start`);
        renderEnrol();
        subscribeEvents();
        goTo("enrol");
    } catch (e) {
        alert(`Could not arm reader:\n${e.message}`);
        btn.disabled = false;
        btn.textContent = "Arm reader & begin";
    }
});

// -------------------- enrol screen --------------------

function renderEnrol() {
    const s = state.session;
    const totalToBind = s.transformed.length;
    state.live = {
        boundIndex: 0,
        skippedCount: 0,
        erroredCount: 0,
        totalToBind,
        rosterState: s.transformed.map(t => ({...t, status: "pending"})),
        alreadyBound: s.already_bound_count,
    };
    $("progress-total").textContent = totalToBind;
    $("progress-done").textContent = 0;
    $("progress-remaining").textContent = "";
    $("next-name").textContent = totalToBind ? s.transformed[0].loxone : "—";
    $("tap-log").innerHTML = "";
    $("status-dot").classList.add("active");
    $("status-dot").title = "armed";
    renderRoster();
    showFocusOverlayIfEnabled();
    updateFocusOverlay();
}

// -------------------- focus overlay --------------------

function showFocusOverlayIfEnabled() {
    // Only show the overlay while the enrol screen is the active one.
    // The toggle checkbox controls whether it's rendered at all.
    if ($("focus-toggle").checked) {
        $("focus-overlay").hidden = false;
    } else {
        $("focus-overlay").hidden = true;
    }
}

function hideFocusOverlay() {
    $("focus-overlay").hidden = true;
}

function updateFocusOverlay() {
    if (!state.live) return;
    const nextPending = state.live.rosterState.find(r => r.status === "pending");
    const nameEl = $("focus-name");
    const nextEl = $("focus-next");
    const progressEl = $("focus-progress");

    // The mockup calls for the roster name (matches what's printed on the
    // card) rather than the loxone name (which is the dot-form). Fall back
    // to loxone if roster is empty for any reason.
    if (nextPending) {
        nameEl.textContent = nextPending.roster || nextPending.loxone;
        nameEl.classList.remove("done", "idle");
        // Look ahead by one for the "up next" line
        const idx = state.live.rosterState.indexOf(nextPending);
        const upNext = state.live.rosterState.slice(idx + 1)
                          .find(r => r.status === "pending");
        nextEl.textContent = upNext
            ? `up next: ${upNext.roster || upNext.loxone}`
            : "last one";
    } else {
        nameEl.textContent = "all done";
        nameEl.classList.add("done");
        nextEl.textContent = "";
    }
    const done = state.live.boundIndex;
    const total = state.live.totalToBind;
    progressEl.textContent = `${done} / ${total} bound`;
}

on($("focus-toggle"), "change", showFocusOverlayIfEnabled);

// Dismiss the overlay without stopping the session — un-ticks the focus
// toggle so the state is consistent for the rest of the session.
on($("focus-dismiss"), "click", () => {
    $("focus-toggle").checked = false;
    hideFocusOverlay();
});

// Stop session from inside the overlay. Delegates to the same handler
// as the main Stop button so behaviour is identical.
on($("btn-focus-stop"), "click", () => $("btn-stop").click());

// Global keyboard shortcuts. Guarded against firing while the operator
// is typing into an input/textarea (they'd expect Enter to do the
// expected in-field thing).
document.addEventListener("keydown", (e) => {
    // Esc — layered: confirm dialog handles its own; then focus overlay;
    // then history panel. Confirm is handled inside confirmAction().
    if (e.key === "Escape") {
        if (!$("focus-overlay").hidden) {
            e.preventDefault();
            $("focus-toggle").checked = false;
            hideFocusOverlay();
            return;
        }
        // history panel Esc handler is separate; already installed.
        return;
    }

    const inTextField = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);

    // Ctrl+K (or Cmd+K on macOS) — toggle history panel.
    if (e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        if (history.open) closeHistory(); else openHistory();
        return;
    }

    // Enter — activate the primary button of the current screen when the
    // operator isn't typing in a field. Suppressed when a modal is open
    // (confirm dialog / history panel).
    if (e.key === "Enter" && !inTextField && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
        if (!$("confirm-scrim").hidden) return;  // let confirm handle it
        if (history.open) return;
        const setupActive = $("screen-setup").classList.contains("active");
        const reviewActive = $("screen-review").classList.contains("active");
        const errActive = $("screen-connect-error").classList.contains("active");
        if (setupActive) {
            const btn = $("btn-review");
            if (!btn.disabled) { e.preventDefault(); btn.click(); }
        } else if (reviewActive) {
            e.preventDefault();
            $("btn-arm").click();
        } else if (errActive) {
            e.preventDefault();
            $("btn-retry-connect").click();
        }
    }
});

function renderRoster() {
    const ul = $("roster-list");
    ul.innerHTML = "";
    for (let i = 0; i < state.live.rosterState.length; i++) {
        const r = state.live.rosterState[i];
        const li = document.createElement("li");
        li.classList.add(r.status);
        if (i === state.live.boundIndex && r.status === "pending") li.classList.add("current");
        const glyph = r.status === "bound"   ? "✓" :
                      r.status === "pre-bound" ? "◈" :
                      i === state.live.boundIndex ? "▸" : "·";
        li.innerHTML = `<span class="glyph">${glyph}</span><span>${escape(r.loxone)}</span>`;
        ul.appendChild(li);
    }
}

function subscribeEvents() {
    const url = `/api/session/${state.session.session_id}/events`;
    const es = new EventSource(url);
    state.live.es = es;

    es.addEventListener("armed",   (e) => logEvent("info", "learn mode armed. tap cards."));
    es.addEventListener("bound",   (e) => onBound(JSON.parse(e.data)));
    es.addEventListener("skipped", (e) => onSkipped(JSON.parse(e.data)));
    es.addEventListener("error",   (e) => onServerError(JSON.parse(e.data)));
    es.addEventListener("reader-error", (e) => onReaderError(JSON.parse(e.data)));
    es.addEventListener("done",    (e) => onDone(JSON.parse(e.data)));
    es.addEventListener("stopped", (e) => onStopped(JSON.parse(e.data)));

    // The SSE stream ends with `null` sentinel on the server; the browser
    // interprets that as a closed connection and will otherwise attempt
    // to reconnect. Guard against that by tracking session state.
    es.onerror = () => {
        // If we've already gone to summary, close cleanly.
        if (["done", "stopped", "server-error"].includes(state.live?.finished)) {
            es.close();
        }
    };
}

function onBound(ev) {
    state.live.boundIndex += 1;
    $("progress-done").textContent = state.live.boundIndex;
    const remaining = state.live.totalToBind - state.live.boundIndex;
    $("progress-remaining").textContent = remaining ? `(${remaining} to go)` : "(done)";
    // mark this row bound; next pending row becomes current
    for (let i = 0; i < state.live.rosterState.length; i++) {
        const r = state.live.rosterState[i];
        if (r.status === "pending" && r.loxone === ev.loxone_name) {
            r.status = "bound";
            break;
        }
    }
    renderRoster();
    const next = state.live.rosterState.find(r => r.status === "pending");
    $("next-name").textContent = next ? next.loxone : "—";
    const mode = ev.dry_run ? "[dry-run]" : "bound";
    logEvent("bound", `${mode.padEnd(10)} ${String(ev.index).padStart(3)}  ${ev.loxone_name.padEnd(24)} ${ev.tag_id}`);
    updateFocusOverlay();
}

function onSkipped(ev) {
    state.live.skippedCount += 1;
    const verb = ev.reason === "locally-bound"
        ? `already bound to ${ev.assigned_to}`
        : `already assigned to ${ev.assigned_to}`;
    logEvent("skipped", `SKIP        —    ${verb}  ${ev.tag_id}`);
}

function onReaderError(ev) {
    state.live.erroredCount += 1;
    logEvent("err", `READER      —    ${ev.reason}  ${ev.tag_id}`);
}

function onServerError(ev) {
    state.live.erroredCount += 1;
    logEvent("err", `ERROR       —    ${ev.reason || "unknown"}  ${ev.roster_name || ""}`);
}

function onDone(ev) {
    state.live.finished = "done";
    $("summary-status").textContent = "COMPLETED";
    $("summary-bound").textContent = ev.bound;
    $("summary-skipped").textContent = ev.skipped;
    $("summary-errored").textContent = ev.errored;
    $("summary-remaining").textContent = state.live.totalToBind - ev.bound;
    $("status-dot").classList.remove("active");
    hideFocusOverlay();
    goTo("summary");
}

function onStopped(ev) {
    state.live.finished = "stopped";
    const bound = state.live.boundIndex;
    $("summary-status").textContent = `STOPPED (${ev.reason})`;
    $("summary-bound").textContent = bound;
    $("summary-skipped").textContent = state.live.skippedCount;
    $("summary-errored").textContent = state.live.erroredCount;
    $("summary-remaining").textContent = state.live.totalToBind - bound;
    $("status-dot").classList.remove("active");
    hideFocusOverlay();
    goTo("summary");
}

on($("btn-stop"), "click", async () => {
    const bound = state.live?.boundIndex ?? 0;
    const total = state.live?.totalToBind ?? 0;
    const remaining = total - bound;
    const message = remaining > 0
        ? `${bound} of ${total} cards bound. Stopping now will end the ` +
          `session with ${remaining} still to do. You can resume later ` +
          `with the same roster — already-bound cards will be skipped.`
        : `The enrolment will end and the reader will be disarmed.`;
    const ok = await confirmAction({
        title: "Stop the enrolment session?",
        message,
        confirmLabel: "Stop session",
        cancelLabel: "Keep going",
    });
    if (!ok) return;
    const btn = $("btn-stop");
    btn.disabled = true;
    btn.textContent = "Stopping…";
    try {
        await apiPost(`/api/session/${state.session.session_id}/stop`);
    } catch (e) {
        alert(`Stop failed:\n${e.message}`);
    } finally {
        btn.disabled = false;
        btn.textContent = "Stop";
    }
});

// -------------------- summary screen --------------------

on($("btn-new-session"), "click", () => {
    if (state.live?.es) state.live.es.close();
    state.session = null;
    state.live = null;
    state.setup.roster = "";
    $("roster-input").value = "";
    hideFocusOverlay();
    updateContinueButton();
    goTo("setup");
});

// -------------------- confirm dialog --------------------

// Show a styled confirmation dialog. Returns a Promise<bool>. The dialog
// defaults to focusing the Cancel button so a stray Enter keypress won't
// accidentally confirm a destructive action. Esc / backdrop click also
// cancel. Enter while Confirm has focus fires the confirm handler.
function confirmAction({title = "Are you sure?", message = "",
                        confirmLabel = "Confirm",
                        cancelLabel = "Cancel"} = {}) {
    return new Promise((resolve) => {
        $("confirm-title").textContent = title;
        $("confirm-message").textContent = message;
        $("confirm-ok").textContent = confirmLabel;
        $("confirm-cancel").textContent = cancelLabel;
        $("confirm-scrim").hidden = false;

        const cleanup = () => {
            $("confirm-scrim").hidden = true;
            $("confirm-ok").removeEventListener("click", onOk);
            $("confirm-cancel").removeEventListener("click", onCancel);
            $("confirm-scrim").removeEventListener("click", onBackdrop);
            document.removeEventListener("keydown", onKey);
        };
        const onOk = () => { cleanup(); resolve(true); };
        const onCancel = () => { cleanup(); resolve(false); };
        const onBackdrop = (e) => {
            if (e.target === $("confirm-scrim")) onCancel();
        };
        const onKey = (e) => {
            if (e.key === "Escape") { e.preventDefault(); onCancel(); }
        };

        $("confirm-ok").addEventListener("click", onOk);
        $("confirm-cancel").addEventListener("click", onCancel);
        $("confirm-scrim").addEventListener("click", onBackdrop);
        document.addEventListener("keydown", onKey);
        // safe default: focus Cancel
        $("confirm-cancel").focus();
    });
}

// -------------------- utilities --------------------

function logEvent(kind, text) {
    const ul = $("tap-log");
    const li = document.createElement("li");
    li.classList.add(kind);
    li.textContent = text;
    ul.insertBefore(li, ul.firstChild);
    while (ul.childElementCount > 30) ul.removeChild(ul.lastChild);
}

function escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;",
        '"': "&quot;", "'": "&#39;",
    })[c]);
}

// -------------------- history slide-out --------------------

const history = {
    open: false,
    detailId: null,
};

function openHistory() {
    $("history-scrim").hidden = false;
    $("history-panel").hidden = false;
    history.open = true;
    showHistoryList();
}
function closeHistory() {
    $("history-scrim").hidden = true;
    $("history-panel").hidden = true;
    history.open = false;
    history.detailId = null;
    // Reset views
    $("history-list").hidden = false;
    $("history-detail").hidden = true;
    $("history-back").hidden = true;
    $("history-title").textContent = "History";
}

async function showHistoryList() {
    history.detailId = null;
    $("history-detail").hidden = true;
    $("history-list").hidden = false;
    $("history-back").hidden = true;
    $("history-title").textContent = "History";

    const listEl = $("history-list");
    listEl.innerHTML = `<div class="history-empty">Loading…</div>`;
    try {
        const rows = await apiGet("/api/sessions");
        if (!rows.length) {
            listEl.innerHTML = `<div class="history-empty">No sessions yet.</div>`;
            return;
        }
        listEl.innerHTML = "";
        for (const row of rows) {
            listEl.appendChild(renderHistoryRow(row));
        }
    } catch (e) {
        listEl.innerHTML = `<div class="history-empty">Error loading history: ${escape(e.message)}</div>`;
    }
}

function renderAuditRow(r) {
    // Glyph + CSS class per status: bound, dry-run, error, pending
    const glyph =
        r.status === "bound"   ? "✓" :
        r.status === "dry-run" ? "◈" :
        r.status === "error"   ? "✗" :
                                  "·";
    const tail = r.status === "pending"
        ? "no tap recorded"
        : (r.tag_id || "");
    return `<li class="${escape(r.status)}">
        <span class="bl-name">${escape(glyph)} ${escape(r.loxone_name)} <span class="bl-roster">(${escape(r.roster_name)})</span></span>
        <span class="bl-tag">${escape(tail)}</span>
    </li>`;
}

function renderHistoryRow(row) {
    const el = document.createElement("div");
    el.classList.add("history-row", row.status);
    const started = new Date(row.started_at);
    const dateStr = started.toLocaleString(undefined, {
        year: "numeric", month: "short", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
    });
    const userDisplay = row.user_name || `<user removed>`;
    const userClass = row.user_name ? "" : "missing";
    const readerDisplay = row.reader_name || row.reader_uuid.slice(0, 12) + "…";
    el.innerHTML = `
        <div class="history-row-top">
            <span class="history-row-date">${escape(dateStr)}</span>
            <span class="history-row-status">${escape(row.status)}</span>
        </div>
        <div class="history-row-user ${userClass}">${escape(userDisplay)}</div>
        <div class="history-row-meta">${escape(readerDisplay)}</div>
        <div class="history-row-counts">
            <span><span class="n ok">${row.bound_count}</span>&nbsp;bound</span>
            <span><span class="n warn">${row.skipped_count}</span>&nbsp;skipped</span>
            <span><span class="n err">${row.errored_count}</span>&nbsp;errored</span>
            <span>of&nbsp;<span class="n">${row.roster_size}</span></span>
        </div>
    `;
    el.addEventListener("click", () => showHistoryDetail(row.id));
    return el;
}

async function showHistoryDetail(sessionId) {
    history.detailId = sessionId;
    $("history-list").hidden = true;
    $("history-detail").hidden = false;
    $("history-back").hidden = false;
    $("history-title").textContent = "Session";

    const detailEl = $("history-detail");
    detailEl.innerHTML = `<div class="history-empty">Loading…</div>`;
    try {
        const data = await apiGet(`/api/sessions/${sessionId}`);
        const s = data.session;
        const started = new Date(s.started_at);
        const ended = s.ended_at ? new Date(s.ended_at) : null;
        const dur = ended ? Math.round((ended - started) / 1000) : null;
        const summaryRows = [
            ["Started",   started.toLocaleString()],
            ["Ended",     ended ? ended.toLocaleString() : "—"],
            ["Duration",  dur !== null ? `${dur}s` : "—"],
            ["Status",    s.status],
            ["Target user", s.user_name || `${s.user_uuid} (deleted)`],
            ["Reader",    s.reader_name || s.reader_uuid],
            ["Roster",    `${s.roster_size} name(s)`],
            ["Bound",     s.bound_count],
            ["Skipped",   s.skipped_count],
            ["Errored",   s.errored_count],
        ];
        const audit = data.roster_audit || [];
        const offRoster = data.off_roster_events || [];
        // Resume is offered when: the session ended prematurely (stopped),
        // we still have the original roster, and at least one row is
        // 'pending' (never got a tap). Completed sessions and sessions
        // predating roster_json (empty audit) don't get the button.
        const pendingCount = audit.filter(r => r.status === "pending").length;
        const canResume = s.status === "stopped" && audit.length > 0 && pendingCount > 0;
        const resumeHtml = canResume ? `
            <div class="history-resume">
                <p class="field-help">
                    ${pendingCount} of ${audit.length} roster entries never got a tap.
                    Resume this session to continue with the remaining names,
                    same target user and reader.
                </p>
                <button class="btn primary" id="btn-resume-${escape(s.id)}">
                    Resume this session
                </button>
            </div>` : "";
        const rosterHtml = audit.length ? `
            <div class="history-detail-bindings">
                <h3>Roster audit (${audit.length})</h3>
                <ul class="binding-list">
                    ${audit.map(r => renderAuditRow(r)).join("")}
                </ul>
            </div>` : "";
        const offRosterHtml = offRoster.length ? `
            <div class="history-detail-bindings">
                <h3>Other cards seen (${offRoster.length})</h3>
                <p class="field-help" style="margin: 0 0 10px;">Cards tapped that were not in the roster — e.g. a colleague's own badge picked up during the session.</p>
                <ul class="binding-list">
                    ${offRoster.map(b => `
                        <li class="${escape(b.status)}">
                            <span class="bl-name">${escape(b.skip_reason || b.status)}</span>
                            <span class="bl-tag">${escape(b.tag_id)}</span>
                        </li>
                    `).join("")}
                </ul>
            </div>` : "";
        // Fallback for legacy sessions (no roster stored): show the raw
        // bindings list like before.
        const fallbackHtml = !audit.length ? `
            <div class="history-detail-bindings">
                <h3>Bindings (${data.bindings.length})</h3>
                <ul class="binding-list">
                    ${data.bindings.map(b => `
                        <li class="${escape(b.status)}">
                            <span class="bl-name">${escape(b.loxone_name || b.skip_reason || "—")}</span>
                            <span class="bl-tag">${escape(b.tag_id)}</span>
                        </li>
                    `).join("") || `<li class="dry-run">no bindings recorded</li>`}
                </ul>
            </div>` : "";
        detailEl.innerHTML = `
            <div class="history-detail-summary summary">
                ${summaryRows.map(([k, v]) => `
                    <div class="summary-row"><span>${escape(k)}</span><span class="value">${escape(String(v))}</span></div>
                `).join("")}
            </div>
            ${resumeHtml}
            ${rosterHtml}
            ${offRosterHtml}
            ${fallbackHtml}
        `;
        if (canResume) {
            const btn = document.getElementById(`btn-resume-${s.id}`);
            if (btn) btn.addEventListener("click", () => resumeSession(data));
        }
    } catch (e) {
        detailEl.innerHTML = `<div class="history-empty">Error: ${escape(e.message)}</div>`;
    }
}

async function resumeSession(data) {
    // Rebuild Setup from the past session's config + remaining roster.
    // The pre-load's already-bound check protects us if the operator
    // taps a card that WAS bound in the previous session, so we don't
    // strictly need to filter — but showing only pending rows in the
    // roster textarea matches the operator's mental model.
    const s = data.session;
    const audit = data.roster_audit || [];
    const remainingRoster = audit
        .filter(r => r.status === "pending")
        .map(r => r.roster_name);

    if (remainingRoster.length === 0) {
        alert("Nothing to resume — no pending rows in that session.");
        return;
    }

    // Verify the reader and user are still visible / exist. If not,
    // we can still resume by preselecting whatever we can; the operator
    // fixes the rest.
    const reader = state.readers.find(r => r.uuid_action === s.reader_uuid);
    const user = state.users.find(u => u.uuid === s.user_uuid);
    const warnings = [];
    if (!reader) warnings.push(`Original reader (${s.reader_name || s.reader_uuid}) is not visible to this account — pick another.`);
    if (!user)   warnings.push(`Original target user (${s.user_name || s.user_uuid}) is no longer in Loxone — pick another.`);

    // Populate setup state + widgets
    if (reader) {
        state.setup.reader_uuid = reader.uuid_action;
        $("reader-select").value = reader.uuid_action;
    }
    if (user) {
        state.setup.user_uuid = user.uuid;
        state.setup.user_name = user.name;
        $("user-filter").value = "";
        renderUsers("");  // re-render full list so the option exists
        $("user-select").value = user.uuid;
    }
    state.setup.roster = remainingRoster.join("\n");
    $("roster-input").value = state.setup.roster;
    updateContinueButton();
    updateRosterCount();

    // Close history, jump to Setup, tell the operator if anything needs
    // their attention.
    closeHistory();
    goTo("setup");
    if (warnings.length) {
        alert("Resumed with warnings:\n\n" + warnings.join("\n"));
    }
}

on($("hamburger-btn"), "click", openHistory);
on($("history-close"), "click", closeHistory);
on($("history-scrim"), "click", closeHistory);
on($("history-back"), "click", showHistoryList);
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && history.open) closeHistory();
});

// -------------------- boot --------------------

goTo("setup");
updateRosterCount();
loadDiscovery();
