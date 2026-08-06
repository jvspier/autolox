"""FastAPI app for the phase-3 web enrolment tool (iteration 1).

Run locally:
    uvicorn autolox_web.app:app --reload

Run in the LXC container:
    uvicorn autolox_web.app:app --host 0.0.0.0 --port 8000

Behind a reverse proxy with a real TLS cert for LAN-wide use.
"""
from __future__ import annotations

import base64
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response

from autolox.client import LoxoneClient, LoxoneError
from autolox.naming import transform_all
from autolox.storage import Store

from autolox_web.deps import load_config, load_web_auth
from autolox_web.models import (
    CreateSessionRequest, CreateSessionResponse, HistoryBindingRow,
    HistorySessionDetail, HistorySessionRow, ReaderInfo, RosterAuditRow,
    RosterEntry, SessionState, UserInfo,
)
from autolox_web.session import Session, SessionManager

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s: %(message)s")
_LOG = logging.getLogger("autolox_web")


_HERE = Path(__file__).parent


class _BasicAuthMiddleware:
    """Gate every request behind one shared username/password.

    Pure ASGI (not Starlette's BaseHTTPMiddleware) so it passes the
    lifespan scope and the /api/session/{id}/events SSE stream straight
    through untouched rather than buffering them - BaseHTTPMiddleware is
    known to interact badly with streaming responses.

    Same threat model as the Loxone visu password (docs/workflow.md's
    Credentials section): one shared credential for whoever is running
    the enrolment session, not per-operator identity.
    """

    def __init__(self, app, username: str, password: str):
        self._app = app
        self._user = username.encode()
        self._pw = password.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        header = dict(scope.get("headers") or []).get(b"authorization", b"")
        if header.startswith(b"Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).partition(b":")
            except Exception:
                user, pw = b"", b""
            if hmac.compare_digest(user, self._user) and hmac.compare_digest(pw, self._pw):
                await self._app(scope, receive, send)
                return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return

        response = Response(status_code=401,
                             headers={"WWW-Authenticate": 'Basic realm="autolox"'})
        await response(scope, receive, send)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """App startup: load config, open store, open Loxone client."""
    cfg = load_config()
    store = Store()
    client = LoxoneClient(
        reader_hosts=cfg.reader_hosts,
        user=cfg.user,
        password=cfg.password,
        visu_password=cfg.visu_password,
        user_host=cfg.user_host,
    )
    manager = SessionManager(store)

    app.state.config = cfg
    app.state.store = store
    app.state.client = client
    app.state.manager = manager

    _LOG.info("autolox_web ready. reader_hosts=%s user_host=%s",
              cfg.reader_hosts, cfg.user_host or cfg.reader_hosts[0])
    try:
        yield
    finally:
        # Best-effort shutdown: stop any active session, close client + store.
        active = manager.active
        if active and active.status == "armed":
            try:
                await manager.stop(client, reason="shutdown")
            except Exception:
                _LOG.exception("shutdown stop failed")
        await client.aclose()
        store.close()


app = FastAPI(title="autolox", lifespan=_lifespan)

_web_user, _web_password = load_web_auth()
if _web_password:
    app.add_middleware(_BasicAuthMiddleware, username=_web_user, password=_web_password)
    _LOG.info("web UI Basic Auth enabled (user=%s)", _web_user)
else:
    _LOG.warning(
        "AUTOLOX_WEB_PASSWORD not set - the web UI has NO authentication. "
        "Anyone who can reach this port can view rosters and bind cards. "
        "Set AUTOLOX_WEB_USER / AUTOLOX_WEB_PASSWORD in .env to enable Basic Auth.")

app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
_templates = Jinja2Templates(directory=_HERE / "templates")


# ---- pages ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _templates.TemplateResponse(request, "index.html", {})


# ---- discovery --------------------------------------------------------------


@app.get("/api/readers", response_model=list[ReaderInfo])
async def readers(request: Request) -> list[ReaderInfo]:
    client: LoxoneClient = request.app.state.client
    try:
        rs = await client.find_readers()
    except LoxoneError as e:
        raise HTTPException(502, f"reader lookup failed: {e}") from e
    return [ReaderInfo(
        uuid_action=r.uuid_action, name=r.name, room=r.room,
        learn_state_uuid=r.learn_state_uuid, owner_host=r.owner_host,
    ) for r in rs]


@app.get("/api/users", response_model=list[UserInfo])
async def users(request: Request) -> list[UserInfo]:
    client: LoxoneClient = request.app.state.client
    try:
        us = await client.get_userlist()
    except LoxoneError as e:
        raise HTTPException(502, f"user lookup failed: {e}") from e
    # The `nfc_tag_count` field is deliberately not surfaced to the
    # frontend — `getuserlist2` always reports zero in our environment
    # (a documented Loxone quirk) and enriching from our own DB would
    # over- or under-count relative to what Loxone actually has.
    # Better silent than confidently wrong. Kept in the response schema
    # for now in case a future Loxone firmware fills it in.
    return [UserInfo(
        uuid=u.uuid, name=u.name, is_admin=u.is_admin,
        nfc_tag_count=u.nfc_tag_count,
    ) for u in us]


# ---- session lifecycle -----------------------------------------------------


@app.post("/api/session", response_model=CreateSessionResponse)
async def create_session(request: Request, body: CreateSessionRequest):
    client: LoxoneClient = request.app.state.client
    store: Store = request.app.state.store
    manager: SessionManager = request.app.state.manager

    # Auto-clear a finished session so the operator can start a new one
    # without hitting a "session already active" 409. Only refuse if the
    # existing session is actually still running.
    if manager.active is not None:
        if manager.active.status in ("completed", "stopped"):
            manager.clear()
        else:
            raise HTTPException(409, "another session is already active")

    # Resolve reader
    try:
        rs = await client.find_readers()
    except LoxoneError as e:
        raise HTTPException(502, f"reader lookup failed: {e}") from e
    reader = next((r for r in rs if r.uuid_action == body.reader_uuid), None)
    if reader is None:
        raise HTTPException(404, "reader not visible to this account")
    if not reader.learn_state_uuid:
        raise HTTPException(400, "reader has no nfcLearnResult state — "
                                  "wrong control?")

    # Resolve user
    try:
        us = await client.get_userlist()
    except LoxoneError as e:
        raise HTTPException(502, f"user lookup failed: {e}") from e
    user = next((u for u in us if u.uuid == body.user_uuid), None)
    if user is None:
        raise HTTPException(404, "user not found")

    # Transform the roster (with warnings surfaced per row)
    rows = transform_all(body.roster)
    unusable = [r for r in rows if not r.loxone]
    if unusable:
        raise HTTPException(400,
                            f"{len(unusable)} row(s) have no loxone name — "
                            "fix the roster before creating a session")

    # Pre-load: which tags are already bound to this user?
    already_bound: set[str] = set()
    # Loxone-side lookup is always attempted — it's authoritative when it
    # works, so we never want to skip it. In practice `getuser/{name}`
    # returns 404 in our environment; we swallow that and fall through.
    try:
        already_bound |= await client.get_user_tags(body.user_uuid)
    except LoxoneError:
        pass
    # Local DB pre-load: skipped when the operator ticked "fresh session"
    # — usually because they've cleared Loxone-side bindings manually and
    # want to re-enrol the same cards without our history blocking them.
    if not body.fresh:
        already_bound |= store.get_bound_tags(body.user_uuid)

    # Persist the session row (in-flight state lives in memory). Include
    # display names so the history view can render even if the user or
    # reader is later renamed or deleted in Loxone, and the transformed
    # roster so audit views can show intended vs actual side by side.
    db_session = store.create_session(
        reader_uuid=body.reader_uuid,
        reader_name=reader.name,
        user_uuid=body.user_uuid,
        user_name=user.name,
        roster_size=len(rows),
        roster=[{"roster": r.roster, "loxone": r.loxone,
                 "warnings": list(r.warnings)} for r in rows],
    )

    session = Session(
        id=db_session.id,
        reader=reader,
        user_uuid=body.user_uuid,
        user_name=user.name,
        transformed=rows,
        dry_run=body.dry_run,
        already_bound=already_bound,
    )
    await manager.create(session)

    reader_info = ReaderInfo(
        uuid_action=reader.uuid_action, name=reader.name, room=reader.room,
        learn_state_uuid=reader.learn_state_uuid, owner_host=reader.owner_host,
    )
    transformed = [RosterEntry(roster=r.roster, loxone=r.loxone,
                                warnings=list(r.warnings)) for r in rows]

    return CreateSessionResponse(
        session_id=db_session.id,
        reader=reader_info,
        user_name=user.name,
        dry_run=body.dry_run,
        transformed=transformed,
        already_bound_count=len(already_bound),
        to_do_count=len(rows),
    )


@app.post("/api/session/{session_id}/start")
async def start_session(request: Request, session_id: str):
    manager: SessionManager = request.app.state.manager
    client: LoxoneClient = request.app.state.client
    cfg = request.app.state.config

    s = manager.active
    if not s or s.id != session_id:
        raise HTTPException(404, "session not found or already ended")
    if s.status != "setup":
        raise HTTPException(409, f"session is {s.status}, cannot start")

    try:
        await manager.start(client, cfg.password)
    except LoxoneError as e:
        raise HTTPException(502, f"could not arm reader: {e}") from e
    return {"status": "armed"}


@app.get("/api/session/{session_id}/events")
async def session_events(request: Request, session_id: str):
    manager: SessionManager = request.app.state.manager
    s = manager.active
    if not s or s.id != session_id:
        raise HTTPException(404, "session not found or already ended")

    async def stream():
        while True:
            event = await s.event_queue.get()
            if event is None:
                break
            yield {"event": event["type"], "data": _json(event)}

    return EventSourceResponse(stream())


@app.post("/api/session/{session_id}/stop")
async def stop_session(request: Request, session_id: str):
    manager: SessionManager = request.app.state.manager
    client: LoxoneClient = request.app.state.client
    s = manager.active
    if not s or s.id != session_id:
        raise HTTPException(404, "session not found or already ended")
    await manager.stop(client, reason="operator")
    manager.clear()
    return {"status": "stopped"}


@app.get("/api/session/{session_id}", response_model=SessionState)
async def get_session(request: Request, session_id: str):
    manager: SessionManager = request.app.state.manager
    s = manager.active
    if not s or s.id != session_id:
        raise HTTPException(404, "session not found")
    reader_info = ReaderInfo(
        uuid_action=s.reader.uuid_action, name=s.reader.name,
        room=s.reader.room, learn_state_uuid=s.reader.learn_state_uuid,
        owner_host=s.reader.owner_host,
    )
    return SessionState(
        session_id=s.id,
        status=s.status,
        dry_run=s.dry_run,
        reader=reader_info,
        user_uuid=s.user_uuid,
        user_name=s.user_name,
        transformed=[RosterEntry(roster=r.roster, loxone=r.loxone,
                                  warnings=list(r.warnings))
                     for r in s.transformed],
        already_bound_count=len(s.already_bound),
        bound_count=s.bound_count,
        skipped_count=s.skipped_count,
        errored_count=s.errored_count,
        remaining_count=len(s.pending),
    )


# ---- history ---------------------------------------------------------------


@app.get("/api/sessions", response_model=list[HistorySessionRow])
async def list_sessions(request: Request, limit: int = 50):
    """List past sessions, newest first. Counts come from the bindings
    table; joined per row rather than a separate call to keep the UI
    simple.

    Sessions created before 0.1.0's schema had `user_name` and
    `reader_name` columns will have those as NULL — we backfill from
    Loxone once and persist, so the panel shows real names instead of
    UUIDs after the first history view."""
    store: Store = request.app.state.store
    client: LoxoneClient = request.app.state.client
    sessions = store.list_sessions(limit=limit)

    if any(s.user_name is None or s.reader_name is None for s in sessions):
        try:
            users = await client.get_userlist()
            user_map = {u.uuid: u.name for u in users}
        except LoxoneError as e:
            _LOG.warning("history backfill: getuserlist2 failed: %s", e)
            user_map = {}
        try:
            readers = await client.find_readers()
            reader_map = {r.uuid_action: r.name for r in readers}
        except LoxoneError as e:
            _LOG.warning("history backfill: find_readers failed: %s", e)
            reader_map = {}
        if user_map or reader_map:
            store.backfill_display_names(
                user_names=user_map, reader_names=reader_map)
            sessions = store.list_sessions(limit=limit)  # re-read

    return [_history_row(store, s) for s in sessions]


@app.get("/api/sessions/{session_id}", response_model=HistorySessionDetail)
async def get_session_history(request: Request, session_id: str):
    store: Store = request.app.state.store
    sess = store.get_session(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    bindings = store.bindings_for_session(session_id)

    # Roster-audit view: for each roster entry, was it bound? Skipped?
    # Errored? Never got a tap? Match by loxone_name, which is what the
    # enroller stamps onto each binding for the row it consumed.
    roster_audit: list[RosterAuditRow] = []
    off_roster: list[HistoryBindingRow] = []
    if sess.roster:
        # Build a loxone_name → matching binding lookup. A roster row can
        # have at most one non-skipped binding (bind consumes it), plus
        # possibly earlier attempts that errored (requeued the name).
        # Skipped/error rows without a roster row are "off-roster".
        by_loxone: dict[str, list] = {}
        for b in bindings:
            if b.loxone_name:
                by_loxone.setdefault(b.loxone_name, []).append(b)

        for entry in sess.roster:
            loxone_name = entry.get("loxone", "")
            matches = by_loxone.get(loxone_name, [])
            # Prefer the terminal state — 'bound' or 'dry-run' or 'error'
            # over an intermediate. If none, 'pending'.
            terminal = next(
                (b for b in matches
                 if b.status in ("bound", "dry-run")),
                None,
            )
            if terminal is None:
                terminal = next(
                    (b for b in matches if b.status == "error"),
                    None,
                )
            if terminal is not None:
                roster_audit.append(RosterAuditRow(
                    roster_name=entry.get("roster", ""),
                    loxone_name=loxone_name,
                    warnings=entry.get("warnings", []),
                    status=terminal.status,
                    tag_id=terminal.tag_id,
                    tag_name=terminal.tag_name,
                    timestamp=terminal.timestamp,
                ))
            else:
                roster_audit.append(RosterAuditRow(
                    roster_name=entry.get("roster", ""),
                    loxone_name=loxone_name,
                    warnings=entry.get("warnings", []),
                    status="pending",
                ))

        # Off-roster: bindings whose loxone_name is empty (i.e., the
        # enroller recorded a skip/error before consuming a roster row).
        for b in bindings:
            if not b.loxone_name:
                off_roster.append(HistoryBindingRow(
                    timestamp=b.timestamp,
                    roster_name=b.roster_name,
                    loxone_name=b.loxone_name,
                    tag_id=b.tag_id,
                    tag_name=b.tag_name,
                    status=b.status,
                    skip_reason=b.skip_reason,
                ))

    return HistorySessionDetail(
        session=_history_row(store, sess, bindings=bindings),
        bindings=[HistoryBindingRow(
            timestamp=b.timestamp,
            roster_name=b.roster_name,
            loxone_name=b.loxone_name,
            tag_id=b.tag_id,
            tag_name=b.tag_name,
            status=b.status,
            skip_reason=b.skip_reason,
        ) for b in bindings],
        roster_audit=roster_audit,
        off_roster_events=off_roster,
    )


def _history_row(store: Store, sess, bindings=None) -> HistorySessionRow:
    """Build the summary row for one session, counting bindings by
    status. Callers may pass the bindings list to avoid a second query
    (used in the detail endpoint)."""
    if bindings is None:
        bindings = store.bindings_for_session(sess.id)
    return HistorySessionRow(
        id=sess.id,
        started_at=sess.started_at,
        ended_at=sess.ended_at,
        status=sess.status,
        reader_uuid=sess.reader_uuid,
        reader_name=sess.reader_name,
        user_uuid=sess.user_uuid,
        user_name=sess.user_name,
        roster_size=sess.roster_size,
        bound_count=sum(1 for b in bindings if b.status == "bound"),
        skipped_count=sum(1 for b in bindings if b.status == "skipped"),
        errored_count=sum(1 for b in bindings if b.status == "error"),
    )


# ---- helpers ---------------------------------------------------------------


def _json(obj) -> str:
    import json as _json_mod
    return _json_mod.dumps(obj, separators=(",", ":"))
