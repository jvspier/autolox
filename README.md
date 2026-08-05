![autolox](docs/hero.png)

# autolox

Bulk NFC card enrolment for Loxone Miniservers. Replaces the manual "one card
at a time in the Loxone web UI" workflow with a tap-and-go tool: tap a card,
it captures the tag ID off the state stream, binds it to the next name in a
roster, moves on.

Written for a seasonal 200-300-worker enrolment cycle, but useful anywhere
you'd otherwise be clicking "Learn NFC Tag" 250 times.

## Two ways to run it

- **Web app** — FastAPI backend + browser UI. Paste roster → pick user +
  reader → review → tap cards. Big legible "next card" overlay for
  head-down work. Session history with roster audit + resume. Meant to run
  in an LXC container or locally.
- **CLI** — same underlying library, single command per session. Handy for
  scripts and quick tests.

## Quick start

Three ways to run it, pick whichever fits your environment.

### Docker (recommended for most people)

```
git clone https://github.com/jvspier/autolox.git
cd autolox
cp .env.example .env       # edit with your Miniserver IPs, service account, passwords
docker compose up -d
```

Open **http://your-docker-host:8000**. See [docs/deploy-docker.md](docs/deploy-docker.md)
for HTTPS, backups, and the fine print.

### LXC on Proxmox

One-shot installer on a fresh Debian/Ubuntu LXC (as root):

```
curl -fsSL https://raw.githubusercontent.com/jvspier/autolox/main/deploy/lxc-install.sh | bash
```

Then edit `/etc/autolox/env` and `systemctl start autolox`. See
[docs/deploy-lxc.md](docs/deploy-lxc.md) for the manual steps and the details.

### Local Python (dev / one-off use)

```
git clone https://github.com/jvspier/autolox.git
cd autolox
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[web]'
cp .env.example .env && chmod 600 .env
# edit .env with your Miniserver IPs, service account, passwords

# web app:
uvicorn autolox_web.app:app --reload --port 8000
# then open http://localhost:8000

# or the CLI:
python loxone_bulk_enroll.py --list         # discover readers
python loxone_bulk_enroll.py --list-users   # discover Loxone users
python loxone_bulk_enroll.py --dry-run \
    --code-touch <reader-uuid> \
    --user-uuid <target-user-uuid> \
    --names names.txt
```

Drop `--dry-run` (or untick "Dry run" in the web UI) to actually bind.

## What's in the box

- `autolox/` — importable package: HTTP + websocket client, crypto, name
  transformation, binary state-stream parser, SQLite storage
- `autolox_web/` — FastAPI backend + vanilla-JS frontend for the browser
  workflow
- `loxone_bulk_enroll.py` — the CLI
- `docs/` — Loxone protocol notes, workflow reasoning, phase-1 verification
  record, phase-3 spec

## Status

- **CLI**: verified end-to-end against real hardware.
- **Web app**: verified end-to-end, including multi-Miniserver Trust
  routing, session history with roster audit + resume, big-format
  "scan the card for" panel for cross-the-room legibility, roster
  paste helpers.
- **LXC deployment**: planned. Dockerfile / systemd / TLS reverse proxy.

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
