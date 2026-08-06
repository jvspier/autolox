"""In-memory session manager for the web app.

One active session at a time (iteration-1 assumption). SSE consumers read
events off `Session.event_queue`; the enroller pushes them there. On stop
or completion the reader is disarmed and the session is closed.

The Store handles durable state (bindings, session records). This module
handles in-flight state (the active learn-mode loop and its SSE stream).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from autolox.client import ERROR_IDS, LoxoneClient, LoxoneError, Reader, TagEvent
from autolox.naming import TransformedName
from autolox.storage import Store

_LOG = logging.getLogger("autolox_web.session")


@dataclass
class Session:
    """One in-flight enrolment session."""
    id: str
    reader: Reader
    user_uuid: str
    user_name: str
    transformed: list[TransformedName]
    dry_run: bool
    already_bound: set[str] = field(default_factory=set)
    status: str = "setup"  # setup | armed | stopped | completed
    pending: list[TransformedName] = field(default_factory=list)
    last_tag: str | None = None
    bound_count: int = 0
    skipped_count: int = 0
    errored_count: int = 0
    # asyncio Queue of event dicts for SSE consumers. Sentinel `None`
    # signals "stream complete".
    event_queue: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=asyncio.Queue,
    )
    _rearm_task: asyncio.Task | None = None
    _consumer_task: asyncio.Task | None = None


class SessionManager:
    """Owns the one-and-only in-flight session."""

    def __init__(self, store: Store):
        self._store = store
        self._session: Session | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> Session | None:
        return self._session

    async def create(self, session: Session) -> None:
        async with self._lock:
            if self._session is not None:
                raise RuntimeError(
                    "another session is already active — stop it first")
            self._session = session

    async def start(self, client: LoxoneClient, session_pw: str) -> None:
        """Arm the reader and begin the SSE tap stream."""
        s = self._require_active()
        s.pending = list(s.transformed)
        s.status = "armed"

        s._rearm_task = asyncio.create_task(_rearm_loop(client, s.reader))
        s._consumer_task = asyncio.create_task(
            _consume_taps(client, s, self._store, session_pw))
        await s.event_queue.put({
            "type": "armed",
            "message": "learn mode armed. tap cards.",
        })

    async def stop(self, client: LoxoneClient, *, reason: str = "operator") -> None:
        s = self._require_active()
        if s.status in ("stopped", "completed"):
            return
        _cancel(s._consumer_task)
        await _finish_session(client, s, self._store, status="stopped")
        await s.event_queue.put({"type": "stopped", "reason": reason})
        await s.event_queue.put(None)  # stream sentinel

    def clear(self) -> None:
        """Drop the reference to the just-finished session so a new one
        can be created."""
        self._session = None

    def _require_active(self) -> Session:
        if self._session is None:
            raise RuntimeError("no active session")
        return self._session


# ---- background loops -----------------------------------------------------


async def _rearm_loop(client: LoxoneClient, reader: Reader,
                      interval: float = 3.0) -> None:
    while True:
        try:
            await client.start_learn(reader)
        except LoxoneError as e:
            _LOG.warning("rearm failed: %s", e)
        await asyncio.sleep(interval)


async def _consume_taps(client: LoxoneClient, s: Session, store: Store,
                        session_pw: str) -> None:
    """Read tag events off the state stream, dispatch to the enroller
    logic (bind / skip / error), push results to the SSE queue."""
    try:
        async for ev in client.subscribe_tags(s.reader, session_pw):
            await _handle_tap(ev, client, s, store)
            if not s.pending:
                await _finish_session(client, s, store, status="completed")
                await s.event_queue.put({
                    "type": "done",
                    "bound": s.bound_count,
                    "skipped": s.skipped_count,
                    "errored": s.errored_count,
                })
                await s.event_queue.put(None)
                return
    except asyncio.CancelledError:
        return
    except Exception as e:
        _LOG.exception("consumer errored")
        await _finish_session(client, s, store, status="stopped")
        await s.event_queue.put({"type": "error", "reason": str(e)})
        await s.event_queue.put(None)


async def _finish_session(client: LoxoneClient, s: Session, store: Store, *,
                          status: str) -> None:
    """Disarm the reader, cancel the rearm loop, mark the DB session ended.

    Called by both the natural-completion path (roster drained) and the
    operator-stop path. Idempotent: repeated calls no-op after the first.
    """
    if s.status in ("stopped", "completed"):
        return
    s.status = status
    _cancel(s._rearm_task)
    try:
        await asyncio.shield(client.stop_learn(s.reader))
    except Exception as e:
        _LOG.warning("stop_learn on %s failed: %s", status, e)
    store.end_session(s.id, status=status)


async def _handle_tap(ev: TagEvent, client: LoxoneClient, s: Session,
                       store: Store) -> None:
    if ev.tag_id == s.last_tag:
        return  # dedupe: same card still on reader
    s.last_tag = ev.tag_id

    if ev.is_error:
        s.last_tag = None
        reason = ERROR_IDS.get(ev.tag_id, "unknown sentinel")
        await s.event_queue.put({
            "type": "reader-error", "tag_id": ev.tag_id, "reason": reason,
        })
        return

    locally_bound = ev.tag_id in s.already_bound
    if ev.is_assigned or locally_bound:
        s.skipped_count += 1
        if ev.is_assigned:
            # State stream says this card belongs to a specific user.
            # ev.tag_name is the Loxone-side per-tag label, which the
            # web UI has enrolled with the user's Loxone-form name
            # ("jan.jansen"). That's actually meaningful here.
            assigned_to = ev.tag_name or ev.user_uuid or "another user"
            reason = "already-assigned"
        else:
            # Locally-bound only — we know from our own DB that this
            # tag belongs to the target user, even though the state
            # stream hasn't caught up yet.
            assigned_to = s.user_name
            reason = "locally-bound"
        store.record_binding(
            session_id=s.id, user_uuid=s.user_uuid,
            roster_name="", loxone_name="",
            tag_id=ev.tag_id, tag_name=ev.tag_name,
            status="skipped",
            skip_reason=reason,
        )
        await s.event_queue.put({
            "type": "skipped",
            "tag_id": ev.tag_id,
            "assigned_to": assigned_to,
            "reason": reason,
        })
        return

    if not s.pending:
        # extra tap, list is empty. Ignore rather than error.
        return

    row = s.pending.pop(0)
    if s.dry_run:
        binding_status = "dry-run"
    else:
        try:
            await client.add_user_nfc(s.user_uuid, ev.tag_id, row.loxone)
        except LoxoneError as e:
            s.errored_count += 1
            s.pending.insert(0, row)  # put it back
            s.last_tag = None
            store.record_binding(
                session_id=s.id, user_uuid=s.user_uuid,
                roster_name=row.roster, loxone_name=row.loxone,
                tag_id=ev.tag_id, tag_name=ev.tag_name,
                status="error", skip_reason=str(e),
            )
            await s.event_queue.put({
                "type": "error",
                "roster_name": row.roster,
                "reason": str(e),
            })
            return
        binding_status = "bound"

    s.already_bound.add(ev.tag_id)
    s.bound_count += 1
    store.record_binding(
        session_id=s.id, user_uuid=s.user_uuid,
        roster_name=row.roster, loxone_name=row.loxone,
        tag_id=ev.tag_id, tag_name=ev.tag_name,
        status=binding_status,
    )
    await s.event_queue.put({
        "type": "bound",
        "index": s.bound_count,
        "roster_name": row.roster,
        "loxone_name": row.loxone,
        "tag_id": ev.tag_id,
        "remaining": len(s.pending),
        "dry_run": s.dry_run,
    })


def _cancel(task: asyncio.Task | None) -> None:
    if task and not task.done():
        task.cancel()
