#!/usr/bin/env python3
"""loxone_bulk_enroll.py - bulk-enrol NFC cards onto a single Loxone user.

Tap card → captured on the state stream → bound to the next name in the queue → next.

Built on the autolox.client library. HTTP for all commands (proven working
end-to-end against a live Miniserver 2026-08-04), websocket for the
nfcLearnResult state stream only.

Credentials and connection config come from a `.env` file, environment
variables, or a getpass prompt (in that priority). Never from argv.

  LOXONE_HOST              LAN IP of the reader-owning Miniserver
  LOXONE_USER_HOST         LAN IP for user ops (defaults to LOXONE_HOST)
  LOXONE_USER              default svc.cardenroll
  LOXONE_PW                login password
  LOXONE_VISU_PW           visualisation password (required for secured commands)
  LOXONE_CODE_TOUCH_UUID   default reader UUID for enrolment
  LOXONE_TARGET_USER_UUID  default target user UUID

Persistence goes to a local SQLite database (default `./autolox.db`,
override via AUTOLOX_DB). Set AUTOLOX_CSV_EXPORT to also append each
binding to a CSV file as a human-readable export.

Usage:
    # 1. discover readers visible to this account
    python loxone_bulk_enroll.py --list

    # 2. dry run — capture tags, write nothing
    python loxone_bulk_enroll.py --dry-run --names names.txt

    # 3. live enrolment
    python loxone_bulk_enroll.py --names names.txt --user-uuid <targetUuid>

If a value is not in `.env` and not in the shell env, the corresponding CLI
flag is required.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import signal
import sys

from dotenv import load_dotenv

from autolox.client import ERROR_IDS, LoxoneClient, LoxoneError, TagEvent
from autolox.naming import TransformedName, transform_all
from autolox.storage import Store

log = logging.getLogger("enrol")


def load_names(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def _parse_hosts(raw: str) -> list[str]:
    """Parse `1.2.3.4,5.6.7.8` into `['1.2.3.4', '5.6.7.8']`. Whitespace
    around commas tolerated. Empty input returns an empty list."""
    return [h.strip() for h in raw.split(",") if h.strip()]


def resolve_credentials(prompt: bool) -> tuple[str, str, str]:
    user = os.getenv("LOXONE_USER", "svc.cardenroll")
    pw = os.getenv("LOXONE_PW")
    visu = os.getenv("LOXONE_VISU_PW")
    if pw is None:
        if not prompt:
            sys.exit("LOXONE_PW not set. Use --prompt or export it.")
        pw = getpass.getpass(f"login password for {user}: ")
    if visu is None:
        if not prompt:
            sys.exit("LOXONE_VISU_PW not set. Use --prompt or export it.")
        visu = getpass.getpass(f"visu password for {user}: ")
    return user, pw, visu


async def list_readers(client: LoxoneClient) -> int:
    readers = await client.find_readers()
    if not readers:
        log.error("no NfcCodeTouch controls visible to this account")
        return 1
    print(f"{'uuidAction':40}  {'name':50}  room")
    for r in readers:
        print(f"{r.uuid_action:40}  {r.name[:50]:50}  {r.room}")
    return 0


async def list_users(client: LoxoneClient, needle: str | None) -> int:
    """Print the user list (uuid, name, admin flag, tag count).

    The underlying client.get_userlist() is the same primitive the phase-3
    web app will call for its dropdown — this is not a CLI-only path.
    """
    users = await client.get_userlist()
    if needle:
        n = needle.lower()
        users = [u for u in users if n in u.name.lower() or n in u.uuid.lower()]
    if not users:
        log.error("no users match %r", needle) if needle else log.error("no users found")
        return 1
    print(f"{'uuid':40}  {'name':30}  admin  tags")
    for u in users:
        print(f"{u.uuid:40}  {u.name[:30]:30}  {'yes  ' if u.is_admin else 'no   '}  {u.nfc_tag_count}")
    return 0


class Enroller:
    """One enrolment session against one reader.

    Persistence goes through `store` (SQLite). No file handles here.
    """

    def __init__(self, client: LoxoneClient, reader,
                 names: list[TransformedName],
                 store: Store, session_id: str,
                 dry_run: bool, force: bool,
                 user_uuid: str | None,
                 already_bound: set[str] | None = None):
        self.client = client
        self.reader = reader
        self.pending: list[TransformedName] = list(names)
        self.store = store
        self.session_id = session_id
        self.dry_run = dry_run
        self.force = force
        self.user_uuid = user_uuid
        self.last_tag: str | None = None
        # Tag IDs already known to be bound to the target user. Seeded from
        # the Miniserver's user record at session start AND from the local
        # DB, augmented as we bind more cards in this session. Needed
        # because the reader's learn-mode state stream doesn't reflect
        # just-made bindings.
        self.already_bound: set[str] = set(already_bound or set())
        self.count = 0

    def close(self) -> None:
        # Nothing to close now — the Store outlives us. Kept for callers
        # that still expect the method during the CLI's shutdown path.
        pass

    async def handle(self, ev: TagEvent) -> None:
        # dedupe: state re-fires while the card is on the reader
        if ev.tag_id == self.last_tag:
            return
        self.last_tag = ev.tag_id

        if ev.is_error:
            log.warning("reader: %s — reseat the card and retry",
                        ERROR_IDS.get(ev.tag_id, "unknown sentinel"))
            self.last_tag = None
            return

        # Already-assigned detection: check state-stream flags AND our
        # locally-tracked bindings. The stream is reliable for tags bound
        # long ago (other users' badges) but stale for freshly-bound tags.
        locally_bound = ev.tag_id in self.already_bound
        if (ev.is_assigned or locally_bound) and not self.force:
            source = ev.tag_name or ev.user_uuid or "target user"
            log.warning("card %s is ALREADY assigned to %s — skipped "
                        "(use --force to rebind)",
                        ev.tag_id, source)
            self.store.record_binding(
                session_id=self.session_id,
                user_uuid=self.user_uuid or "",
                roster_name="", loxone_name="",
                tag_id=ev.tag_id, tag_name=ev.tag_name,
                status="skipped",
                skip_reason=("already-assigned"
                             if ev.is_assigned else "locally-bound"),
            )
            return

        if not self.pending:
            log.info("card %s tapped but the name list is exhausted",
                     ev.tag_id)
            return

        row = self.pending.pop(0)
        self.count += 1

        if self.dry_run:
            log.info("[dry-run] %3d  %-30s  %s",
                     self.count, row.loxone, ev.tag_id)
            binding_status = "dry-run"
        else:
            try:
                await self.client.add_user_nfc(self.user_uuid, ev.tag_id,
                                                row.loxone)
            except LoxoneError as e:
                log.error("addusernfc failed for %s: %s — REQUEUING name",
                          ev.tag_id, e)
                self.pending.insert(0, row)
                self.count -= 1
                self.last_tag = None
                self.store.record_binding(
                    session_id=self.session_id,
                    user_uuid=self.user_uuid or "",
                    roster_name=row.roster, loxone_name=row.loxone,
                    tag_id=ev.tag_id, tag_name=ev.tag_name,
                    status="error", skip_reason=str(e),
                )
                return
            log.info("bound     %3d  %-30s  %s",
                     self.count, row.loxone, ev.tag_id)
            binding_status = "bound"

        # Track locally so a re-tap in this session is caught even if the
        # reader's state cache hasn't refreshed yet.
        self.already_bound.add(ev.tag_id)
        self.store.record_binding(
            session_id=self.session_id,
            user_uuid=self.user_uuid or "",
            roster_name=row.roster, loxone_name=row.loxone,
            tag_id=ev.tag_id, tag_name=ev.tag_name,
            status=binding_status,
        )

        remaining = len(self.pending)
        if remaining:
            log.info("          next: %s   (%d to go)",
                     self.pending[0].loxone, remaining)
        else:
            log.info("          list complete — %d cards enrolled",
                     self.count)


def preview_and_confirm(rows: list[TransformedName], *, assume_yes: bool) -> bool:
    """Print the two-column roster→loxone table with per-row warnings and
    ask the operator to confirm. Returns True to proceed, False to abort.

    Hard requirement from CLAUDE.md and docs/workflow.md — never derive
    Loxone names in code without showing the operator."""
    print()
    print("name transformation preview:")
    max_r = max((len(r.roster) for r in rows), default=10)
    max_l = max((len(r.loxone) for r in rows), default=10)
    header = f"  {'roster'.ljust(max_r)}  ->  {'loxone'.ljust(max_l)}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    any_warn = False
    for row in rows:
        mark = "!" if row.has_warnings else " "
        print(f"{mark} {row.roster.ljust(max_r)}  ->  {row.loxone.ljust(max_l)}")
        for w in row.warnings:
            any_warn = True
            print(f"    ↳ {w}")
    print()
    if any_warn:
        print("! some rows have warnings. Review them before proceeding.")
    if assume_yes:
        print("--yes given: proceeding without prompt.")
        return True
    ans = input("looks OK? proceed? [y/N] ").strip().lower()
    return ans in ("y", "yes")


async def rearm_loop(client: LoxoneClient, reader, interval: float = 3.0):
    while True:
        try:
            await client.start_learn(reader)
        except LoxoneError as e:
            log.error("rearm failed: %s", e)
        await asyncio.sleep(interval)


async def enrol_session(client: LoxoneClient, args, pw: str) -> int:
    readers = await client.find_readers()
    reader = next((r for r in readers if r.uuid_action == args.code_touch), None)
    if reader is None:
        log.error("no reader with uuidAction %s (visible to this account)",
                  args.code_touch)
        return 1
    if not reader.learn_state_uuid:
        log.error("reader has no nfcLearnResult state — wrong control?")
        return 1

    raw_names = load_names(args.names)
    if not raw_names:
        log.error("no names in %s", args.names)
        return 2
    rows = transform_all(raw_names)

    log.info("reader : %s (%s)", reader.name, reader.uuid_action)
    log.info("names  : %d loaded", len(rows))
    log.info("mode   : %s",
             "DRY RUN — nothing will be written" if args.dry_run else "LIVE")

    if not preview_and_confirm(rows, assume_yes=args.yes):
        log.info("aborted by operator, nothing sent to the Miniserver.")
        return 0

    # sanity: refuse to proceed if any row has an unrecoverable warning
    unusable = [r for r in rows if not r.loxone]
    if unusable:
        log.error("%d row(s) have no loxone name — fix names.txt and retry",
                  len(unusable))
        return 3

    store = Store()
    already_bound: set[str] = set()
    if args.user_uuid:
        # Path 1: try to fetch from the Miniserver. Fails silently on Trust
        # routing quirks (getuser/{name} returns 404 for Trust-shared users
        # on some Miniserver firmwares).
        try:
            from_server = await client.get_user_tags(args.user_uuid)
            already_bound |= from_server
            log.info("server: target user has %d bound tag(s)",
                     len(from_server))
        except LoxoneError as e:
            log.warning("could not fetch existing tags via API: %s", e)
            log.warning("falling back to local DB for pre-load")

        # Path 2: always also load from the local DB. Covers cards bound in
        # earlier sessions when the getuser endpoint isn't reachable. Cheap
        # and idempotent — the two sets are unioned.
        from_db = store.get_bound_tags(args.user_uuid)
        new_from_db = from_db - already_bound
        if new_from_db:
            log.info("db: adding %d additional tag(s) from local storage",
                     len(new_from_db))
        already_bound |= from_db

        if already_bound:
            log.info("total: %d tag(s) will be treated as already-assigned",
                     len(already_bound))
        else:
            log.info("no prior bindings for this user — every tap will "
                     "consume a name")

    # Record this session in the DB. The session record links every
    # binding together for later summaries and for cross-session
    # resumability of the "180 done / 70 to go" flow.
    session = store.create_session(
        reader_uuid=reader.uuid_action,
        user_uuid=args.user_uuid or "",
        roster_size=len(rows),
    )
    log.info("session: %s", session.id)

    enroller = Enroller(client, reader, rows, store, session.id,
                        args.dry_run, args.force, args.user_uuid,
                        already_bound=already_bound)

    rearm = asyncio.create_task(rearm_loop(client, reader))

    async def consume():
        async for ev in client.subscribe_tags(reader, pw):
            await enroller.handle(ev)

    consumer = asyncio.create_task(consume())

    log.info("learn mode armed. Tap cards now. Ctrl-C when done.")
    log.info("          next: %s", rows[0].loxone if rows else "-")

    # Install signal handlers that cancel the consumer task on Ctrl-C.
    # Python 3.14's asyncio.run doesn't propagate KeyboardInterrupt as a
    # task CancelledError reliably, so finally-block cleanup gets skipped
    # unless we do this explicitly.
    loop = asyncio.get_running_loop()
    interrupted = False

    def _cancel_on_signal():
        nonlocal interrupted
        interrupted = True
        consumer.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _cancel_on_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler; on that platform
            # we fall back to Python's default KeyboardInterrupt behaviour.
            pass

    try:
        await consumer
    except asyncio.CancelledError:
        pass
    finally:
        rearm.cancel()
        try:
            await rearm
        except (asyncio.CancelledError, Exception):
            pass
        # asyncio.shield: if the task itself gets cancelled again during
        # cleanup, we still want stop_learn to complete. Otherwise the
        # reader stays armed until its hardcoded timeout — as happened
        # 2026-08-04 before this fix.
        try:
            await asyncio.shield(client.stop_learn(reader))
            log.info("stop_learn OK")
        except asyncio.CancelledError:
            log.warning("stop_learn cancelled — reader may still be armed")
        except Exception as e:
            log.warning("stop_learn on shutdown: %s", e)
        enroller.close()
        # Mark the session finished. 'completed' if we drained the roster,
        # 'stopped' if the operator interrupted us or the list was longer
        # than the taps.
        end_status = ("completed"
                      if interrupted is False and not enroller.pending
                      else "stopped")
        store.end_session(session.id, status=end_status)
        store.close()
        if interrupted:
            log.info("interrupted by signal.")

    return 0


async def async_main(args) -> int:
    user, pw, visu = resolve_credentials(prompt=args.prompt)
    reader_hosts = _parse_hosts(args.host)
    async with LoxoneClient(reader_hosts, user, pw, visu,
                             user_host=args.user_host) as c:
        if args.list:
            return await list_readers(c)
        if args.list_users:
            return await list_users(c, args.user_filter)
        if not args.code_touch:
            log.error("--code-touch is required (or use --list to discover)")
            return 2
        if not args.names:
            log.error("--names is required for enrolment")
            return 2
        if not args.dry_run and not args.user_uuid:
            log.error("--user-uuid is required unless --dry-run")
            return 2
        return await enrol_session(c, args, pw)


def main() -> int:
    # Load .env from CWD (or wherever LOXONE_ENV_FILE points). Does not
    # override values already in os.environ, so shell env still wins over
    # the file. Idempotent — safe to call multiple times.
    env_file = os.environ.get("LOXONE_ENV_FILE", ".env")
    load_dotenv(env_file, override=False)

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host",
                   default=os.getenv("LOXONE_HOSTS") or os.getenv("LOXONE_HOST"),
                   help="Miniserver LAN address(es) for reader ops. Accepts a "
                        "single host or comma-separated list — each host is "
                        "queried for its own NfcCodeTouch controls and reader "
                        "commands route to whichever host reported the reader. "
                        "Env: LOXONE_HOSTS (list) or LOXONE_HOST (single)")
    p.add_argument("--user-host", default=os.getenv("LOXONE_USER_HOST"),
                   help="Miniserver LAN address to use for user operations "
                        "(getuserlist2, getuser, addusernfc). In a Trust cluster "
                        "this should be where the target user object lives. "
                        "Defaults to --host if omitted. Env: LOXONE_USER_HOST")
    p.add_argument("--code-touch", default=os.getenv("LOXONE_CODE_TOUCH_UUID"),
                   help="uuidAction of the NFC Code Touch to enrol on. "
                        "Env: LOXONE_CODE_TOUCH_UUID")
    p.add_argument("--user-uuid", default=os.getenv("LOXONE_TARGET_USER_UUID"),
                   help="UUID of the target Loxone user for addusernfc. "
                        "Env: LOXONE_TARGET_USER_UUID")
    p.add_argument("--names", help="text file, one name per line, in tap order")
    p.add_argument("--list", action="store_true",
                   help="list visible NFC Code Touches and exit")
    p.add_argument("--list-users", action="store_true",
                   help="list Loxone users (uuid, name, admin, tag count) and exit")
    p.add_argument("--user-filter",
                   help="with --list-users, substring filter on name or uuid")
    p.add_argument("--dry-run", action="store_true",
                   help="capture tags, journal them, write nothing to Loxone")
    p.add_argument("--force", action="store_true",
                   help="rebind cards already assigned (use with care)")
    p.add_argument("--yes", action="store_true",
                   help="skip the interactive confirm on the name transformation preview")
    p.add_argument("--prompt", action="store_true",
                   help="prompt for passwords not present in env "
                        "(LOXONE_PW, LOXONE_VISU_PW). Default: on when stdin is a tty")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if not args.host:
        p.error("--host is required (or set LOXONE_HOSTS / LOXONE_HOST in "
                ".env / environment)")

    # sensible default: prompt if stdin is a tty and no env creds
    if not args.prompt and sys.stdin.isatty():
        args.prompt = True

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nstopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
