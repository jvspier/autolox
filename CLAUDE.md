# CLAUDE.md

Project instructions for Claude Code. Read `docs/protocol.md` before writing any
code that talks to the Miniserver.

## What this project is

A tool to bulk-enrol NFC access cards onto a Loxone Miniserver. Replaces a manual,
one-card-at-a-time process - repeated hundreds of times per batch - with a tap-and-go
workflow.

Context: any organization that brings on a batch of people needing building access at
once - a seasonal hiring surge, an event, a cohort of contractors - ends up handing out
a stack of printed DESFire EV3 keycards that must open doors controlled by Loxone.
Today an admin stands at a wall-mounted NFC Code Touch, uses "NFC Tag Aanleren" in the
Loxone web UI, taps one card, types the person's name, submits. Once per card, hundreds
of times per batch.

## Current status - read this first

**Phase-1 CLI is proven end-to-end against real hardware (2026-08-04).** All four
steps in `docs/phase-1.md` pass: reader discovery, 5-card dry-run, 5-card live
enrolment to a real Loxone user, and re-tap detection. The `nfc/startlearn` visu-
secured command works, learn mode stays armed continuously (~20 min windows tested),
`addusernfc` writes are visible in Loxone Config afterwards, and re-tapping a bound
card is caught by the safety check.

The code has enrolled real cards on real hardware; it has NOT yet been used for a
full production batch of 200-300 cards. That's the production distinction — until
that has happened, describe as "proven, awaiting first production run" rather than
"production-ready".

**Web app is built, deployed, and stability-tested (2026-08-05/06).** Docker/Podman
and Debian 13 LXC deployment paths both verified on real hardware. A ~101-card dry
run held stable for 40+ minutes on the LXC (no memory growth, no re-arm gaps, both
already-assigned detection layers exercised for real). The web UI now sits behind a
login page (`AUTOLOX_WEB_PASSWORD` in `.env`) after a security review found every
route was reachable with zero credential - see the Authentication section in
`docs/deploy-lxc.md` / `docs/deploy-docker.md`. A same-day code-review pass also
fixed a real exception-handling bug in `autolox/client.py` (HTTP failures weren't
being converted to the client's own `LoxoneError`, so the documented Trust-cluster
`getuser` 404 fallback silently never fired) - see `docs/protocol.md`'s Connect
handshake section for a related crypto gotcha (encrypted-command salt must stay
fixed per session) found and fixed the same way.

## Architecture

- `autolox/` — importable Python package.
  - `client.py` — `LoxoneClient`, the async client. HTTP for all commands (including
    secured NFC learn-mode signed with locally-computed `visu_hash`). Websocket used
    only for the `nfcLearnResult` state stream. Takes two hosts: `reader_host`
    (owner of the target reader) and `user_host` (where the target user lives) —
    matters for Trust clusters, degenerates cleanly when both are the same.
  - `crypto.py` — RSA/AES/HMAC primitives for the Loxone websocket handshake.
  - `statestream.py` — parser for Loxone's binary event tables (text-states only;
    that's what carries `nfcLearnResult`).
  - `naming.py` — roster → Loxone name transformation. Uses `unidecode` (per the
    gotcha in `docs/workflow.md`). Surfaces collisions to a human, never
    auto-numbers silently.
- `loxone_bulk_enroll.py` — thin CLI on top of the client. Env-var / `.env` config
  supported; passwords never in argv.
- `.env.example` — connection/credential template. Copy to `.env`, chmod 600.

`pyloxone-api` was tried and removed. Its encrypted-websocket dispatch of secured
commands is broken (silent drop, empirically 2026-08-04). We kept its binary parser
as reference for `statestream.py`, credited at the top of the file.

## Hard constraints

- **Never invent Loxone endpoints.** `docs/protocol.md` lists every endpoint known to
  exist, and how it was verified. If something is needed that isn't in that list,
  say so rather than guessing a plausible-looking path. Loxone's API is only partly
  documented and hallucinated endpoints fail in confusing ways.
- **Scope: enrolment only.** The tool does not create Loxone users, does not delete
  anything, does not print cards, does not talk to the backoffice software, and does
  not talk to Google Sheets. Its only write operation is `addusernfc` against one
  user UUID supplied at runtime. Keep it that way - the small blast radius is a
  deliberate design property, not an accident.
- **Never derive Loxone usernames in code without showing the operator.** See the
  naming rules in `docs/workflow.md`. The transformation is presented for review and
  confirmation before any binding happens.
- **Do not add a stop/start learn cycle per card.** Continuous learn mode with a 3s
  re-arm is verified working (2026-08-04). The official web UI stops after each
  card because its UI is single-shot; we deliberately do not.
- **Already-assigned detection is layered.** `nfcLearnResult`'s `userUuid` field is
  authoritative for cards bound *long ago* but STALE for cards freshly bound via
  `addusernfc` (Loxone's own UI uses an internal enrolment path that atomically
  refreshes the reader cache; `addusernfc` alone does not). The tool combines the
  state-stream check with a client-side "already bound" set seeded from the CSV
  journal at startup. Don't remove either half.

## Environment

- Python 3.10+ (targets modern async but avoids 3.14-specific features so the same
  code runs on a Debian/Ubuntu LXC container).
- Runtime deps: `httpx`, `websockets` (>=13, <14 — must have `.legacy` submodule if
  we ever go back to a legacy WS client, though we don't currently), `pycryptodome`,
  `unidecode`, `python-dotenv`. Pinned in `pyproject.toml`.
- On Fedora with Python 3.14: `pip install legacy-cgi` is needed as a compatibility
  shim (Python 3.13+ removed the `cgi` stdlib module; some old deps still import it).
- Miniserver reachable on TCP 80 over LAN. Always use the LAN address, never
  `dyndns.loxonecloud.com` - no cloud round-trip, no remote connection slot consumed,
  much lower latency when doing hundreds of cards.
- Trust cluster support: in a multi-Miniserver Trust setup, `user_host` should
  point at the Miniserver where the target user lives; `reader_host` (aka
  `--host`) should point at the Miniserver that OWNS the target reader. Tree
  devices only respond on their owning Miniserver.
- Web app (`autolox_web/`): FastAPI backend + browser frontend, built and
  deployed (Docker/Podman and Debian 13 LXC both verified). Phase-1 CLI runs
  identically in an LXC or on a laptop.

## Conventions

- Keep the core enrolment logic importable. The CLI is one caller; the web app
  is another. The CLI must remain functional as a debugging tool.
- Credentials never in argv for anything beyond bench testing - they land in shell
  history and are visible in `ps`. Use env vars, a `0600` config file, or a prompt.
- Journal every card as it is processed, not at the end of the run. If a
  session dies at card 180 the journal is the only record of what actually happened.
- Sessions must be resumable. State lives in the Miniserver (via the
  already-assigned check) and the journal, never in memory only.
- Filter the four error sentinel IDs before treating anything as a card. They are
  listed in `docs/protocol.md` and they are not tags.

## Where things are

- `docs/protocol.md` — Loxone wire protocol, verified endpoints, provenance of each fact
- `docs/workflow.md` — business process, naming rules, product decisions and why
- `docs/phase-1.md` — original test plan; largely resolved as of 2026-08-04
- `autolox/` — Python package (see Architecture above), including
  `storage.py` — SQLite journal (`autolox.db` by default), the source of
  truth for the already-bound check and session resumability. CSV export
  is optional (`AUTOLOX_CSV_EXPORT`), not the primary journal.
- `autolox_web/` — FastAPI backend (`app.py`, `session.py`, `deps.py`,
  `models.py`) + Jinja2/vanilla-JS frontend (`templates/`, `static/`).
  Shares `autolox/` for everything Loxone-related; the web layer is a thin
  adapter, same as the CLI.
- `loxone_bulk_enroll.py` — CLI entrypoint on top of `autolox.client`
- `.env` / `.env.example` — connection & credentials config
