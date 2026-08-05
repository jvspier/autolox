"""Pydantic request/response models for the web API."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---- discovery -----------------------------------------------------------


class ReaderInfo(BaseModel):
    uuid_action: str
    name: str
    room: str
    learn_state_uuid: str
    owner_host: str


class UserInfo(BaseModel):
    uuid: str
    name: str
    is_admin: bool
    nfc_tag_count: int


# ---- session create --------------------------------------------------------


class RosterEntry(BaseModel):
    roster: str
    loxone: str
    warnings: list[str]


class CreateSessionRequest(BaseModel):
    reader_uuid: str = Field(..., description="uuidAction of the target reader")
    user_uuid: str = Field(..., description="UUID of the target Loxone user")
    roster: list[str] = Field(..., min_length=1,
                               description="Roster names, one per row")
    dry_run: bool = False
    fresh: bool = Field(
        default=False,
        description="If true, skip local DB pre-load. Use when Loxone-side "
                    "bindings have been cleared manually and the local DB "
                    "history is stale.",
    )


class CreateSessionResponse(BaseModel):
    session_id: str
    reader: ReaderInfo
    user_name: str
    dry_run: bool
    transformed: list[RosterEntry]
    already_bound_count: int
    to_do_count: int


# ---- session state ---------------------------------------------------------


class SessionState(BaseModel):
    """Current state snapshot — used to hydrate the UI on refresh
    (iteration 2 feature; endpoint exists in iteration 1 for read-only
    inspection)."""
    session_id: str
    status: str  # 'setup' | 'armed' | 'stopped' | 'completed'
    dry_run: bool
    reader: ReaderInfo
    user_uuid: str
    user_name: str
    transformed: list[RosterEntry]
    already_bound_count: int
    bound_count: int
    skipped_count: int
    errored_count: int
    remaining_count: int


# ---- errors ----------------------------------------------------------------


class ErrorResponse(BaseModel):
    error: str


# ---- history ---------------------------------------------------------------


class HistorySessionRow(BaseModel):
    """One row in the history list."""
    id: str
    started_at: str
    ended_at: str | None
    status: str
    reader_uuid: str
    reader_name: str | None
    user_uuid: str
    user_name: str | None
    roster_size: int
    bound_count: int
    skipped_count: int
    errored_count: int


class HistoryBindingRow(BaseModel):
    """One row in the per-session detail view."""
    timestamp: str
    roster_name: str
    loxone_name: str
    tag_id: str
    tag_name: str | None
    status: str
    skip_reason: str | None


class RosterAuditRow(BaseModel):
    """One row in the roster-audit view of a session.
    Combines the *intended* roster entry with what actually happened.
    Status values: 'bound' | 'dry-run' | 'error' | 'pending' (never got
    a tap). loxone_name is authoritative if present; roster_name is the
    original human form."""
    roster_name: str
    loxone_name: str
    warnings: list[str] = []
    status: str
    tag_id: str | None = None
    tag_name: str | None = None
    timestamp: str | None = None


class HistorySessionDetail(BaseModel):
    session: HistorySessionRow
    bindings: list[HistoryBindingRow]
    # roster-audit view: intended enrollees joined with what happened.
    # Empty for legacy sessions (created before the roster_json column).
    roster_audit: list[RosterAuditRow] = []
    # Bindings whose roster_name is blank — cards tapped that weren't in
    # the roster, e.g. a colleague tapping their own badge. Skipped or
    # errored, never bound.
    off_roster_events: list[HistoryBindingRow] = []
