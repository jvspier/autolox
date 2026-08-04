"""LoxoneClient — HTTP command layer plus websocket state subscription.

All *commands* go over HTTP with basic auth. Secured commands are signed
locally with visu_hash (see crypto.py) — proven working end-to-end against a
live Miniserver on 2026-08-04. We deliberately avoid pyloxone-api's
encrypted-websocket command dispatch, which is broken for secured commands.

The *state stream* (nfcLearnResult tag events) needs a websocket, because
those states are complex JSON that HTTP polling can't observe. The websocket
does its own auth handshake (keyexchange → getkey2 → getjwt) and then just
subscribes to state updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import quote

import httpx
import websockets

_LOG = logging.getLogger("autolox.client")

from autolox.crypto import Session, normalise_public_key, password_hash, \
    rsa_encrypt_session_key, visu_hash
from autolox.statestream import MessageType, parse_header, parse_text_states

# Sentinel IDs the reader emits instead of a real tag. Filter before binding.
# Sourced from LearnNfcTagScreen.NFC_ERROR_IDS in Loxone comps.js.
ERROR_IDS: dict[str, str] = {
    "00 00 00 00 00 00 00 00 E8": "read error",
    "00 00 00 00 00 00 00 00 EE": "authentication error (card not writable?)",
    "00 00 00 00 00 00 00 00 EF": "init error",
    "00 00 00 00 00 00 00 00": "invalid tag",
}


@dataclass(frozen=True)
class Reader:
    """One NfcCodeTouch control from LoxAPP3.json."""
    uuid_action: str
    name: str
    room: str
    learn_state_uuid: str  # nfcLearnResult state UUID; where tag IDs arrive
    device_state_uuid: str | None


@dataclass(frozen=True)
class LoxoneUser:
    """One user record from getuserlist2, in a form both the CLI and the
    future web-app dropdown can consume without re-parsing.

    `raw` holds the full dict for anything not surfaced as a typed field —
    the Miniserver's user shape is not fully documented so we intentionally
    don't strip it."""
    uuid: str
    name: str
    is_admin: bool
    nfc_tag_count: int
    raw: dict


@dataclass(frozen=True)
class TagEvent:
    """One nfcLearnResult event decoded from the state stream."""
    tag_id: str
    tag_name: str
    user_uuid: str | None
    device_uuid: str | None

    @property
    def is_error(self) -> bool:
        return self.tag_id in ERROR_IDS

    @property
    def is_assigned(self) -> bool:
        return bool(self.user_uuid) or bool(self.device_uuid)


class LoxoneClient:
    """Thin async client for the enrolment workflow.

    Two hosts are supported to accommodate Loxone Trust clusters:
    - `reader_host` receives NFC learn-mode commands and hosts the state
      stream websocket. Must be the Miniserver that physically owns the
      target reader (Tree devices only respond on their owning Miniserver).
    - `user_host` receives `getuserlist2`, `getuser`, and `addusernfc`.
      Should be the Miniserver where the Trust users live. Empirically
      `getuser/{name}` returns 404 when queried on a peer for a user local
      to another Miniserver, so this routing distinction matters.

    If `user_host` is omitted, both operations use `reader_host` (backward
    compatible for standalone Miniservers or single-host testing).

    Usage:
        async with LoxoneClient(reader_host="192.0.2.10",
                                 user_host="192.0.2.1", ...) as c:
            readers = await c.find_readers()
            await c.start_learn(readers[0].uuid_action)
            ...
    """

    def __init__(self, reader_host: str, user: str, password: str,
                 visu_password: str | None = None, *,
                 user_host: str | None = None,
                 timeout: float = 10.0):
        self._host = reader_host
        self._user_host = user_host or reader_host
        self._user = user
        self._visu = visu_password
        # httpx client for reader ops (NFC commands, structure discovery,
        # secured commands, and the initial getPublicKey for the websocket
        # handshake — all Miniserver-local operations).
        self._http = httpx.AsyncClient(
            base_url=f"http://{reader_host}",
            auth=(user, password),
            timeout=timeout,
        )
        # httpx client for user ops. When the two hosts are equal this is
        # a separate client instance but functionally identical — the
        # small overhead beats branching every call.
        self._user_http = httpx.AsyncClient(
            base_url=f"http://{self._user_host}",
            auth=(user, password),
            timeout=timeout,
        )

    async def __aenter__(self) -> "LoxoneClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()
        await self._user_http.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._user_http.aclose()

    # ---- structure & discovery -----------------------------------------

    async def structure(self) -> dict:
        r = await self._http.get("/data/LoxAPP3.json")
        r.raise_for_status()
        return r.json()

    async def find_readers(self, structure: dict | None = None) -> list[Reader]:
        s = structure or await self.structure()
        rooms = s.get("rooms") or {}
        out: list[Reader] = []
        for uuid, ctrl in (s.get("controls") or {}).items():
            if ctrl.get("type") != "NfcCodeTouch":
                continue
            states = ctrl.get("states") or {}
            learn = states.get("nfcLearnResult")
            if not learn:
                continue  # can't enrol on a reader we can't listen to
            room = rooms.get(ctrl.get("room"), {}).get("name", "")
            out.append(Reader(
                uuid_action=ctrl.get("uuidAction", uuid),
                name=ctrl.get("name", "?"),
                room=room,
                learn_state_uuid=learn,
                device_state_uuid=states.get("deviceState"),
            ))
        return out

    async def get_user_detail(self, name: str) -> dict:
        """Fetch full user detail (including nfcTags list) via getuser/{name}.

        `getuserlist2` is summary-only — it does NOT include the nfcTags
        array despite what its name suggests. Use this method when you need
        the tag list, or any other per-user detail not visible on the list.

        Routes to `user_host`. Trust routing across Miniservers is unreliable
        for this endpoint, so this must hit the Miniserver where the user
        lives."""
        r = await self._user_http.get(f"/jdev/sps/getuser/{quote(name)}")
        r.raise_for_status()
        j = r.json()["LL"]
        if str(j.get("Code") or j.get("code")) != "200":
            raise LoxoneError(f"getuser({name!r}) failed: {j}")
        val = j.get("value")
        if isinstance(val, str):
            val = json.loads(val)
        if not isinstance(val, dict):
            raise LoxoneError(f"getuser({name!r}) unexpected shape: {type(val)}")
        return val

    async def get_user_tags(self, user_uuid: str) -> set[str]:
        """Return the set of NFC tag IDs currently bound to a user.

        Used by the enrolment tool to seed its already-bound set — the
        reader's learn-mode state stream doesn't reflect just-made bindings
        (empirically confirmed 2026-08-04: userUuid stays null in
        nfcLearnResult for freshly-enrolled tags), so we can't rely on
        state-stream assignment flags alone.

        Tries `getuser/{name}` first, then `getuser/{uuid}`. Some firmwares
        accept one and not the other, and this endpoint has generally been
        unreliable in our testing — callers should be prepared to fall
        back to another source (e.g., the local CSV journal).
        """
        users = await self.get_userlist()
        target = next((u for u in users if u.uuid == user_uuid), None)
        if target is None:
            raise LoxoneError(f"user {user_uuid} not found in getuserlist2")
        detail: dict | None = None
        errors: list[str] = []
        for identifier in (target.name, user_uuid):
            try:
                detail = await self.get_user_detail(identifier)
                break
            except LoxoneError as e:
                errors.append(f"{identifier!r}: {e}")
        if detail is None:
            raise LoxoneError("getuser failed for all identifiers: " + " | ".join(errors))
        tags = detail.get("nfcTags") or []
        ids: set[str] = set()
        for t in tags:
            if isinstance(t, dict):
                tid = t.get("id") or t.get("tagId") or t.get("nfcId")
                if tid:
                    ids.add(str(tid))
            elif isinstance(t, str):
                ids.add(t)
        return ids

    async def get_userlist(self) -> list[LoxoneUser]:
        """Fetch getuserlist2 and return typed users, sorted by name.

        Used by the CLI (--list-users) and by the phase-3 web app's user
        dropdown. Keep the return shape stable — it's a semi-public API.
        Routes to `user_host` so we see the Trust cluster's canonical user
        list rather than a peer's filtered view.
        """
        r = await self._user_http.get("/jdev/sps/getuserlist2")
        r.raise_for_status()
        j = r.json()["LL"]
        if str(j.get("Code") or j.get("code")) != "200":
            raise LoxoneError(f"getuserlist2 failed: {j}")

        raw_users = j.get("value")
        # getuserlist2's value may be a list, or a JSON string of a list —
        # Loxone endpoints have both forms in the wild. Handle both.
        if isinstance(raw_users, str):
            try:
                raw_users = json.loads(raw_users)
            except (ValueError, TypeError) as exc:
                raise LoxoneError(f"getuserlist2 value not JSON: {exc}") from exc
        if not isinstance(raw_users, list):
            raise LoxoneError(f"getuserlist2 unexpected shape: {type(raw_users)}")

        users: list[LoxoneUser] = []
        for u in raw_users:
            if not isinstance(u, dict):
                continue
            tags = u.get("nfcTags") or []
            users.append(LoxoneUser(
                uuid=str(u.get("uuid", "")),
                name=str(u.get("name", "")),
                is_admin=bool(u.get("isAdmin") or u.get("admin") or False),
                nfc_tag_count=len(tags) if isinstance(tags, list) else 0,
                raw=u,
            ))
        users.sort(key=lambda x: x.name.lower())
        return users

    # ---- secured commands (HTTP path) ----------------------------------

    async def _visu_signed(self, uuid_action: str, cmd: str) -> str:
        """Fetch a fresh salt and build a signed jdev/sps/ios/... URL."""
        if not self._visu:
            raise LoxoneError("visu password is required for secured commands")
        r = await self._http.get(f"/jdev/sys/getvisusalt/{self._user}")
        r.raise_for_status()
        val = r.json()["LL"]["value"]
        h = visu_hash(self._visu, val["key"], val["salt"],
                       val.get("hashAlg", "SHA256"))
        return f"/jdev/sps/ios/{h}/{uuid_action}/{cmd}"

    async def start_learn(self, uuid_action: str) -> None:
        url = await self._visu_signed(uuid_action, "nfc/startlearn")
        r = await self._http.get(url)
        _check_ll(r, "startlearn")

    async def stop_learn(self, uuid_action: str) -> None:
        url = await self._visu_signed(uuid_action, "nfc/stoplearn")
        r = await self._http.get(url)
        _check_ll(r, "stoplearn")

    # ---- binding -------------------------------------------------------

    # ---- state stream (websocket) --------------------------------------

    async def subscribe_tags(self, reader: "Reader", password: str,
                              *, keepalive_seconds: float = 120.0,
                              ) -> AsyncIterator["TagEvent"]:
        """Open a websocket to this Miniserver, complete auth, subscribe to
        the state stream, and yield a TagEvent for each nfcLearnResult update
        on the given reader.

        The `password` argument duplicates what LoxoneClient already knows
        because httpx.AsyncClient.auth doesn't expose the plaintext password
        cleanly; passing it explicitly is simpler than reaching into the
        auth object.

        The subscription runs until the caller cancels it. Errors during the
        handshake raise LoxoneError; errors during subscription raise
        websockets.exceptions or LoxoneError.
        """
        pk_resp = await self._http.get("/jdev/sys/getPublicKey")
        pk_resp.raise_for_status()
        pk = pk_resp.json()["LL"]["value"]
        public_key_pem = normalise_public_key(pk)

        session = Session()
        rsa_blob = rsa_encrypt_session_key(public_key_pem, session.key, session.iv)

        ws_url = f"ws://{self._host}/ws/rfc6455"
        async with websockets.connect(
            ws_url, subprotocols=["remotecontrol"], max_size=2**22,
        ) as ws:
            # 1. keyexchange (unencrypted)
            await ws.send(f"jdev/sys/keyexchange/{rsa_blob}")
            await _expect_ll(ws, "jdev/sys/keyexchange")

            # 2. getkey2 — user's login-hash salt+key
            await ws.send(session.encrypt_command(f"jdev/sys/getkey2/{self._user}"))
            resp = await _expect_ll(ws, "jdev/sys/getkey2")
            key_wire = resp["key"]
            salt_wire = resp["salt"]
            hash_alg = resp.get("hashAlg", "SHA256")

            # 3. getjwt — fresh JWT with permission 2 (web). State stream
            #    subscription only needs a valid session; no user-management
            #    right needed for read-only observation.
            pw_hash = password_hash(self._user, password, key_wire, salt_wire, hash_alg)
            client_uuid = "edfc5f9a-df3f-4cad-9dddcdc42c732be2"
            client_info = "autolox"
            permission = 2
            jwt_cmd = (f"jdev/sys/getjwt/{pw_hash}/{self._user}/{permission}/"
                       f"{client_uuid}/{client_info}")
            await ws.send(session.encrypt_command(jwt_cmd))
            await _expect_ll(ws, "jdev/sys/getjwt")

            # 4. enablebinstatusupdate — flip on the state stream
            await ws.send(session.encrypt_command("jdev/sps/enablebinstatusupdate"))
            await _expect_ll(ws, "dev/sps/enablebinstatusupdate")

            # 5. main loop: header+payload frame pairs, yield matching events
            last_keepalive = asyncio.get_event_loop().time()
            while True:
                now = asyncio.get_event_loop().time()
                if now - last_keepalive >= keepalive_seconds:
                    await ws.send("keepalive")
                    last_keepalive = now

                try:
                    header_bytes = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue  # go back and check keepalive

                if not isinstance(header_bytes, (bytes, bytearray)):
                    continue  # skip stray text frames (rare)

                header = parse_header(header_bytes)
                if header.payload_length == 0:
                    # keepalive header alone, or out-of-service. Loop.
                    if header.message_type == MessageType.OUT_OF_SERVICE:
                        raise LoxoneError("Miniserver reported out of service")
                    continue

                payload = await ws.recv()
                if header.message_type != MessageType.TEXT_STATES:
                    continue  # we only care about text-state tables
                if not isinstance(payload, (bytes, bytearray)):
                    continue

                events = parse_text_states(bytes(payload))
                learn_text = events.get(reader.learn_state_uuid)
                if not learn_text:
                    continue
                _LOG.debug("nfcLearnResult: %s", learn_text)
                try:
                    parsed = json.loads(learn_text)
                except (ValueError, TypeError):
                    continue
                tag_id = str(parsed.get("id") or "").strip()
                if not tag_id:
                    continue
                yield TagEvent(
                    tag_id=tag_id,
                    tag_name=str(parsed.get("name") or ""),
                    user_uuid=parsed.get("userUuid") or None,
                    device_uuid=parsed.get("deviceUuid") or None,
                )

    # ---- binding -------------------------------------------------------

    async def add_user_nfc(self, user_uuid: str, tag_id: str,
                           tag_name: str) -> None:
        """Bind a tag to a user. Not a secured command — plain HTTP GET.

        Loxone's own web UI calls this twice per card: once to bind, once to
        rename. Re-invoking with the same tag_id updates the name — that is
        the documented repair path for a mis-bound card.

        Routes to `user_host` — the write goes to the user's home Miniserver
        (Trust propagation of `addusernfc` was verified working, but we
        still prefer to write at the source rather than rely on it).
        """
        path = (f"/jdev/sps/addusernfc/{user_uuid}"
                f"/{quote(tag_id)}/{quote(tag_name)}")
        r = await self._user_http.get(path)
        _check_ll(r, "addusernfc")


# ---- helpers -----------------------------------------------------------


class LoxoneError(RuntimeError):
    """Raised when the Miniserver returns a non-200 LL code."""


def _check_ll(response: httpx.Response, label: str) -> None:
    response.raise_for_status()
    j = response.json().get("LL", {})
    code = str(j.get("Code") or j.get("code") or "")
    if code != "200":
        raise LoxoneError(f"{label} returned code {code!r}: {j}")


async def _expect_ll(ws, label: str) -> dict:
    """Read one header + one text response from a Loxone websocket.
    Verify LL.code == 200 and return the LL.value (parsed if dict, else {})."""
    header_bytes = await ws.recv()
    header = parse_header(bytes(header_bytes))
    # for text responses payload is a separate frame; for typed state
    # tables the payload is binary. During the handshake we only ever
    # expect TEXT responses, so read the next frame regardless.
    payload = await ws.recv()
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", errors="replace")
    try:
        j = json.loads(payload)["LL"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LoxoneError(f"{label}: unparseable response {payload!r}") from exc
    code = str(j.get("Code") or j.get("code") or "")
    if code != "200":
        raise LoxoneError(f"{label} returned code {code!r}: {j}")
    val = j.get("value")
    return val if isinstance(val, dict) else {}
