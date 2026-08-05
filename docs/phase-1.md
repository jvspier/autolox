# Phase 1 - proven

**Status: complete.** All four verification steps below passed end-to-end against
real hardware (Loxone Config 17.1.1). This document is kept as historical context
and to describe how the tool was verified — future edits should preserve
provenance.

Do NOT run these steps unnecessarily against production readers. They arm learn
mode, which visibly affects the reader. The tests below have been performed; the
tool is ready for a real seasonal batch.

## Prerequisites (as recorded from the original run)

- Service account (e.g. `svc.cardenroll`) created in Loxone Config with User
  Management right, "Web Interface" access enabled, login password AND visu
  password set, added to a user group that has access to the target reader,
  saved to Miniserver.
- Python 3.10+ with `autolox` package installable — `pip install -e .` from the
  repo root, or dependencies installed into a venv (see CLAUDE.md).
- Miniserver **LAN** address, not `dyndns.loxonecloud.com`.
- 5 blank DESFire cards + a names file with 5 fake names (`test-names.txt`).
- A Code Touch on a non-critical door for the test. In our environment this was
  an interior technical-room reader on a peer Miniserver in the Trust cluster.

## Test sequence (all steps passed)

Each step is cheap to re-run if regressions are suspected.

### 1. `--list` — PASSED

```bash
python loxone_bulk_enroll.py --list
```

Proves: connectivity, HTTP basic auth, `LoxAPP3.json` fetch, and that
`find_readers()` correctly matches `NfcCodeTouch` controls and extracts their
`nfcLearnResult` state UUIDs.

Result: one reader row for the target, with correct `uuidAction` and learn-state
UUID.

### 2. Five-card dry run — PASSED

```bash
python loxone_bulk_enroll.py --dry-run --names test-names.txt
```

Proves the two things that matter most:

- **`nfc/startlearn` as a secured HTTP command from a third-party client is
  accepted.** Sends `jdev/sps/ios/{visuHash}/{uuid}/nfc/startlearn` — the
  Miniserver arms the reader on the first call.
- **Learn mode stays armed continuously.** The tool re-sends `startlearn` every
  3s. Five distinct tag IDs captured, five rows in `enrolled.csv`, zero gaps.

Nothing written to the Miniserver during this step.

### 3. Same five cards, live — PASSED

```bash
python loxone_bulk_enroll.py --names test-names.txt --user-uuid <target>
```

Proves: `addusernfc` is accepted by svc.cardenroll's HTTP basic auth. No token
permission escalation needed — the User-Management right on the account is
enough. `--token-permission` did not need to be adjusted.

Verified in Loxone Config afterwards: five tags visible on the target user with
the correct loxone-form names (`jan.jansen`, `anne.de.wit`, etc.). One cosmetic
note — Loxone's UI shows the first entry capitalised (`Jan.Jansen`); the stored
name is correct as sent.

### 4. Re-tap one of the enrolled cards — PASSED

```bash
python loxone_bulk_enroll.py --dry-run --names test-names.txt --user-uuid <target>
```

Then tap an already-enrolled card. The pending queue does not advance.

Two mechanisms combine to detect this:
- The state-stream `userUuid` field flags cards bound to OTHER users (e.g., a
  colleague's badge). Verified against a real employee's keycard — was caught
  with the correct assigned user and name reported by the reader.
- A CSV journal pre-load, plus in-session tag tracking, catches cards bound to
  the target user (the state stream is stale for freshly-bound target-user
  tags — see "Loxone quirks" below).

## Loxone quirks discovered during verification

These are worth preserving so they don't get re-discovered later.

### `nfcLearnResult` is stale for freshly-bound target-user cards

After `addusernfc` succeeds, the `nfcLearnResult` state stream still reports
`userUuid: null` and `name: "NFC ID N"` (Loxone's default written-on-card name)
for the just-bound card. Loxone's own web UI apparently uses an internal
enrolment path that refreshes the reader's cache atomically; `addusernfc` alone
does not. The client-side already-bound set is not optional — it is the safety
mechanism against consuming a name for a re-tap.

### `getuser/{name}` returns LL 404 in our environment

Despite being documented, the endpoint returns 404 for our target user on both
Miniservers we tried, regardless of whether we ask by name or by UUID. Possibly a
permission-tier issue for the service account, possibly firmware. The tool falls
back to the CSV journal for pre-load; behaviour is correct either way.

### Trust routing differs by endpoint

`addusernfc` Trust-routes cleanly — you can send it to any Miniserver in the
cluster and it lands on the right user record. Reader commands
(`nfc/startlearn`, state stream) DO NOT Trust-route reliably; they must go to
the reader-owning Miniserver. The client accepts two hosts (`reader_host` and
`user_host`) to accommodate this.

### Python 3.14 asyncio + KeyboardInterrupt

`asyncio.run()` on Python 3.14 doesn't reliably propagate `KeyboardInterrupt`
as a task `CancelledError`, so finally-block cleanup got skipped and left the
reader armed until its hardcoded timeout. Fixed by installing an explicit
`loop.add_signal_handler` that cancels the consumer task, plus `asyncio.shield`
around the `stop_learn` call. If you see the reader still armed after Ctrl-C in
future, look here.

## Non-decisions worth preserving

- **We do NOT rely on the ordering of the `nfcTags` array from `getuser`.**
  Whether it is insertion-ordered is unknown, so any design that zips it against
  a name list positionally can silently mis-bind every card. Bind at tap time
  instead.

## Definition of done — met

- ✓ Five cards enrolled from the CLI in a single session, verified in Config
- ✓ Learn-mode continuity confirmed
- ✓ Credentials moved out of argv (env vars, `.env`, or getpass)
- ○ 250-name stability dry-run — remains as an optional smoke test. Doesn't
  need real cards or a reader — just a long-running dry-run to check the tool
  doesn't drop the websocket or leak resources.
