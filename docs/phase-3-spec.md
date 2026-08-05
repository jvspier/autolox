# Phase 3 — web app spec

Iteration 1 of a phased build-out. Goal: a working browser UI that mirrors what
the CLI does today, running as a FastAPI service that can be deployed to an LXC
container on Proxmox.

**Camera / OCR (originally iteration 3): abandoned 2026-08-05.** Prototyped
with Tesseract.js client-side; recognition quality on glossy printed PVC
cards under office lighting was well below what would make the feature
worth its speed cost. The current tap-in-order flow is fast enough that
the camera-verifier's original justification (catch shuffled cards) doesn't
justify the added latency + failure modes. If we ever revisit: server-side
cloud OCR (Google Vision / AWS Textract), or QR/barcode on the card
artwork, are the two viable paths — both replace OCR entirely rather than
tuning it.

Read this doc, mark it up with any changes / questions before implementation
starts. Iteration boundaries are deliberate — resist creep.

## Goals for iteration 1

1. **Web UI** that walks an operator through Setup → Review → Enrol → Summary.
2. **FastAPI backend** wrapping `autolox.client` — thin adapter, no business
   logic duplicated.
3. **Reader + user dropdowns** on Setup, populated from `find_readers()` and
   `get_userlist()`. Not deferred — text-field UUIDs are hostile UX and we
   already have the primitives.
4. **Live tap stream** via Server-Sent Events (SSE) from backend → browser.
   Each tap becomes a UI update.
5. **Dark theme, Loxone-influenced visual language.** Not a 1:1 clone; same
   feel — dark canvas, green accent header, rounded cards, minimal chrome.
6. **Runs identically on laptop and in an LXC container.** `uvicorn` locally,
   same command in the container. No OS-specific dependencies.

## Non-goals (explicitly deferred)

- Camera / OCR verifier — iteration 3.
- Multi-operator sessions — iteration 1 assumes a single-operator single-tab
  model. Second concurrent operator is undefined behaviour.
- Authentication on the web app itself. Behind a private network / LXC-only
  reachability for now. Auth added when we open it beyond LAN.
- Undo of a bound tag. `--force` behaviour is available via a "rebind" toggle
  later; iteration 1 skips it.
- Progress persistence across browser refresh. If the browser closes mid-run,
  the operator can re-open, the enrolment continues on the server, but state
  reconciliation on reconnect is iteration-2 work.

## Architecture

```
browser  ─── HTTP  ──►  FastAPI app  ─── autolox.client ───►  Loxone Miniserver
   ▲                       │
   └──  SSE (tap stream) ──┘
```

- **One FastAPI process** owns the `LoxoneClient` instance and any in-flight
  learn-mode session. Single-operator assumption means we can hold session
  state as a single module-level object (`SessionManager` in `autolox_web/session.py`).
- **SSE, not WebSocket**, for the tap stream. Server → browser is one-way;
  SSE is simpler, works through HTTP intermediaries, auto-reconnects in the
  browser for free. `sse-starlette` gives us an `EventSourceResponse`.
- **Between browser and server**: REST-ish JSON. POST to start a session,
  GET/SSE to observe, POST to confirm each step.

## Endpoints

| Method | Path                              | Purpose |
|--------|-----------------------------------|---------|
| GET    | `/`                               | Serves the single-page frontend |
| GET    | `/api/readers`                    | List NfcCodeTouch readers (`find_readers()`) |
| GET    | `/api/users`                      | List Loxone users (`get_userlist()`) |
| POST   | `/api/session`                    | Create a session: `{reader_uuid, user_uuid, roster: [str]}` → returns `{session_id, transformed: [{roster, loxone, warnings}]}` |
| POST   | `/api/session/{id}/start`         | Confirm the preview, arm learn mode. 200 on success. |
| GET    | `/api/session/{id}/events`        | **SSE stream** of tap/bind events. |
| POST   | `/api/session/{id}/stop`          | Send stoplearn, close session. |
| GET    | `/api/session/{id}`               | Current state snapshot (for refresh recovery in iteration 2). |

Every response wraps errors as `{error: "message"}` with an appropriate HTTP
status. No stack traces to the browser.

## SSE event schema

Every event is a JSON object on the stream:

```json
{"type": "bound",   "index": 3, "loxone_name": "anne.de.wit", "tag_id": "...", "remaining": 47}
{"type": "skipped", "reason": "already-assigned", "tag_id": "...", "assigned_to": "jan.jansen"}
{"type": "error",   "reason": "read-error", "tag_id": "..."}
{"type": "done",    "count": 5, "csv_path": "enrolled.csv"}
{"type": "stopped", "reason": "operator-halt"}
```

Frontend keeps an ordered log; each event type maps to a distinct row style.

## Session lifecycle

```
setup ──► review ──► enrolling ──► done
              │           │
              └── abort ──┘
```

- **setup**: dropdowns populated, operator picks reader + user, pastes roster,
  presses Continue.
- **review**: server has transformed names, returned preview + warnings.
  Operator confirms; POST to `/start`.
- **enrolling**: learn mode armed, SSE stream live, each tap updates UI.
  Operator can press Stop at any point. Stop = hard stop: reader disarmed,
  session closed, summary shown.
- **done**: reached end of roster OR operator stopped. Summary rendered
  from server-side session state.

**Cross-session resumability.** The tool has no "pause + continue same
session" mode — that would be brittle (Loxone hardcodes a learn-mode
timeout that would fight the pause). Instead: hard stop, then on the NEXT
session, the Setup screen shows "X of Y already bound to <target user>,
resume the remaining Z". This is powered by the pre-load mechanism we
already built for the CLI (CSV journal + `getuser` if reachable). Operator
never has to remember which batch they were on.

**Browser tab refresh mid-enrolment** is NOT handled in iteration 1. If
the tab reloads during active enrolment, the server-side session keeps
running but the browser loses its view. Worst case: reader stays armed
until its hardcoded timeout, operator opens the app again and picks up
via the "resume the remaining Z" flow. Iteration 2 adds tab reconnect
via a session ID in localStorage.

## Session storage (iteration 1)

**In-flight session state** is in-process, keyed by session_id (UUID4). One
active session at a time is enforced — creating a second while one is active
returns 409. Simplifies everything.

**Durable state (bindings + session records)** lives in **SQLite**, not CSV.
The CLI is migrated to the same storage in this iteration. Rationale in
"Storage decisions" below.

Restart of the FastAPI process = in-flight session lost, but everything
already-bound is durable in the DB. Next session reads the DB via the
pre-load and resumes with the "180 done / 70 to go" summary.

## Storage decisions

Primary store: SQLite (`autolox.db`). CSV becomes an OPTIONAL export
artifact (for the "paste back into the Google Sheet" workflow in
`docs/workflow.md`).

**Why SQLite over CSV:**
- Right shape for the queries we already need (already-bound tag IDs for
  the pre-load, session summaries for the web UI, per-user filtering).
- Stdlib (`sqlite3`) — zero new dependencies.
- Single-file, trivially backed up, easy to inspect
  (`sqlite3 autolox.db '.schema'`).
- Row-level durability via journaling. Better than "did the CSV flush"
  guessing.
- The CLI and web app share the same primitive; no divergence between
  them.

**Config env vars:**
- `AUTOLOX_DB` — path to SQLite file. Default `./autolox.db` in dev,
  `/var/lib/autolox/autolox.db` in the LXC deployment.
- `AUTOLOX_CSV_EXPORT` — optional. If set, every successful bind is ALSO
  appended to this CSV. If unset, no CSV written. The DB is always
  authoritative.

**Schema:**

```sql
CREATE TABLE sessions (
    id           TEXT PRIMARY KEY,       -- UUID4
    started_at   TEXT NOT NULL,          -- ISO 8601
    ended_at     TEXT,                   -- null while active
    reader_uuid  TEXT NOT NULL,
    user_uuid    TEXT NOT NULL,
    roster_size  INTEGER NOT NULL,
    status       TEXT NOT NULL           -- 'active' | 'completed' | 'stopped'
);

CREATE TABLE bindings (
    id            INTEGER PRIMARY KEY,
    timestamp     TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    user_uuid     TEXT NOT NULL,         -- redundant with sessions but speeds pre-load
    roster_name   TEXT NOT NULL,         -- "Jan Jansen"
    loxone_name   TEXT NOT NULL,         -- "jan.jansen"
    tag_id        TEXT NOT NULL,
    tag_name      TEXT,                  -- "NFC ID 430" from state stream
    status        TEXT NOT NULL,         -- 'bound' | 'dry-run' | 'skipped' | 'error'
    skip_reason   TEXT
);

CREATE INDEX idx_bindings_user_status ON bindings(user_uuid, status);
CREATE INDEX idx_bindings_session     ON bindings(session_id);
```

**Migration from the old CSV:** existing `enrolled.csv` rows on a running
installation get imported into the DB on first startup if the DB is empty
and the CSV exists. Idempotent — the DB is the source of truth after
import. In our specific case the current CSV is fake test data, so we
just delete it and start clean.

Storage lives in `autolox/storage.py` (part of the core package, not the
web-only module) so CLI and web share one implementation.

## Frontend structure

Vanilla HTML + CSS + a small vanilla-JS module. No framework. The whole flow
is 4 screens on one page; a framework would be more machinery than the
problem needs, and no build step means the LXC deploy stays trivial.

```
autolox_web/
├── app.py               # FastAPI entrypoint
├── session.py           # SessionManager + per-session state
├── deps.py              # LoxoneClient lifetime (FastAPI dependency)
├── models.py            # Pydantic request/response models
├── static/
│   ├── style.css        # dark Loxone-esque theme
│   ├── app.js           # SPA-lite: view switching + SSE consumer
│   └── icons/           # SVG icons for readers/users/status
└── templates/
    └── index.html       # single page, four <section>s
```

## Visual design

Loxone Web UI 17+ style. Not a copy — recognisable family resemblance.

- **Canvas**: very dark, near-black (`#0f1114`).
- **Header**: full-width green bar (`#8cff6f`, Loxone's brand green),
  ~64px tall. Left: workspace name (e.g. "autolox"). Center: title of
  current step. Right: clock + status dot.
- **Cards**: dark rounded rectangles (`#1e2126`, radius ~12px, generous
  padding). Icon left, text center-left, action right.
- **Bottom nav**: 4 dots or icons for Setup / Review / Enrol / Summary,
  active step highlighted. Non-clickable during enrolment (can only
  advance by finishing / stopping).
- **State colors** on enrolment cards:
  - grey — waiting
  - amber — matched, ready to tap (iteration 3)
  - green — bound
  - red — error / already-assigned
- **Typography**: system font stack (`-apple-system, "Segoe UI", Inter, ...`).
- **No emoji, no gradients.** Solid colors, sharp text, generous whitespace.

## Deployment

**Dev on the laptop:**

```bash
pip install -e .[web]                    # web extras group
uvicorn autolox_web.app:app --reload
```

Browser opens `http://localhost:8000`.

**LXC container:**

```bash
uvicorn autolox_web.app:app --host 0.0.0.0 --port 8000
```

Behind an internal reverse proxy with a real TLS cert (needed later for
`getUserMedia` in iteration 3; harmless in iteration 1). Systemd unit file
ships with the code so `systemctl enable autolox` works in the container.

## Dependencies added

- `fastapi` — the framework.
- `uvicorn[standard]` — ASGI server.
- `sse-starlette` — SSE support.
- `pydantic>=2` — request/response models (FastAPI pulls this transitively).
- `jinja2` — template rendering.

All in a new `[project.optional-dependencies].web` group. `pip install -e .`
still installs just the CLI; `pip install -e .[web]` picks up the web bits.

## Open questions / to decide

1. **Whether to gate `/api/users` and `/api/readers` behind session creation.**
   Currently spec has them as free reads. If a security layer arrives later
   we'll rethink; iteration 1 keeps them open.
3. **CSV output path in the container.** Currently `./enrolled.csv` relative
   to CWD. In LXC we'd want an explicit persistent volume path
   (`/var/lib/autolox/enrolled.csv`). Configurable via `AUTOLOX_JOURNAL_PATH`.
4. **Roster side panel during enrolment.** Show the full roster with a
   status glyph next to each row (waiting / bound / skipped)? Cheap and
   informative — recommend yes for iteration 1.

## What iteration 1 does NOT prove

Deliberate to keep the scope closed:

- That the LXC deploy actually works — that's iteration 4.
- That the browser flow scales to 250 real cards in one session — but a
  desk-side 10-tap test proves the mechanism.
- That the visual design lands. First render will look approximate; polish
  in iteration 2.

## Estimated size

Ballpark to keep expectations grounded:

- `app.py` — ~100 LOC
- `session.py` — ~150 LOC
- `models.py` — ~60 LOC
- `deps.py` — ~40 LOC
- `static/app.js` — ~200 LOC
- `static/style.css` — ~250 LOC
- `templates/index.html` — ~120 LOC

Total ~900 LOC of new code plus test/fixture bits. Reasonable for one focused
session once the spec is approved.
