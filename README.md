# autolox

Bulk NFC card enrolment for Loxone Miniservers. Replaces the manual "one card at a
time in the Loxone web UI" workflow with a tap-and-go CLI: tap a card, it captures
the tag ID off the state stream, binds it to the next name in a roster, moves on.

Written for a seasonal 200-300-worker enrolment cycle, but useful anywhere you'd
otherwise be clicking "NFC Tag aanleren" 250 times.

## Quick start

```
git clone https://github.com/<you>/autolox.git
cd autolox
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env && chmod 600 .env
# edit .env with your Miniserver IP, service account, passwords
python loxone_bulk_enroll.py --list         # discover readers you can see
python loxone_bulk_enroll.py --list-users   # discover Loxone users
python loxone_bulk_enroll.py --dry-run \
    --code-touch <reader-uuid> \
    --user-uuid <target-user-uuid> \
    --names names.txt
```

Add `--verbose` for the protocol trace. Drop `--dry-run` to actually bind.

## What's in the box

- `autolox/` — importable package: HTTP + websocket client, crypto, name
  transformation, binary state-stream parser
- `loxone_bulk_enroll.py` — the CLI
- `docs/` — protocol notes, workflow reasoning, phase-1 verification record

## Status

Phase-1 CLI is verified end-to-end against real hardware; ready for a first
seasonal run. Phase-2 (camera-verified enrolment) and phase-3 (FastAPI web app
for LXC hosting) are next.

See [CLAUDE.md](CLAUDE.md) for the project brief, hard constraints, and where
things live. See [docs/](docs/) for the detailed reasoning behind each design
decision and the Loxone protocol reverse-engineering notes.

## Security

- Loxone service account should have User Management right and specific access
  to the reader(s) used for enrolment — nothing more.
- Credentials go in `.env` (chmod 600, gitignored) or environment variables.
  Never on argv.
- The tool's only write endpoint is `addusernfc` against one runtime-supplied
  user UUID. Small blast radius by design.
