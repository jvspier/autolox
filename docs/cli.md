# CLI reference

`loxone_bulk_enroll.py` is the command-line interface. It's a thin wrapper
around the same `autolox` library the web app uses — same functionality,
same env-var configuration, different UX. Handy for:

- Scripting or automation
- Debugging a session without the web app running
- Discovering what your service account can see on the Loxone side

The web app (`autolox_web`) is the recommended interface for real
enrolment sessions. Use the CLI when you want raw control or a scripted
step.

## Quick start

Every command reads the same `.env` file / environment variables the web
app uses. See `.env.example` for the required variables (host list,
credentials, etc).

```bash
# discover which readers your service account can see
python loxone_bulk_enroll.py --list

# discover Loxone users
python loxone_bulk_enroll.py --list-users

# dry run — capture tag IDs to enrolled.csv / DB, write NOTHING to Loxone
python loxone_bulk_enroll.py --dry-run \
    --code-touch <reader-uuid> \
    --names names.txt

# real enrolment — attaches tapped tags to the given Loxone user
python loxone_bulk_enroll.py \
    --code-touch <reader-uuid> \
    --user-uuid <target-user-uuid> \
    --names names.txt
```

Ctrl-C at any point to stop. On stop, the reader is disarmed and the
session is closed cleanly.

## Flags

Grouped by role. Everything except the flags in **Session** can be set
via env vars (in `.env` or the shell) instead of passing on the command
line.

### Connection

| Flag | Env var | Notes |
|---|---|---|
| `--host <ip>[,ip...]` | `LOXONE_HOSTS` or `LOXONE_HOST` | Reader-owning Miniserver(s). Comma-separated for a Trust cluster. Required. |
| `--user-host <ip>` | `LOXONE_USER_HOST` | Miniserver where the target user object lives. Defaults to `--host` if omitted. |
| `--prompt` | — | Prompt for any password not present in the env. Default: on when stdin is a tty. |

Credentials come from env vars only (never on argv):

| Env var | Notes |
|---|---|
| `LOXONE_USER` | Service account name (default `svc.cardenroll`) |
| `LOXONE_PW` | Login password |
| `LOXONE_VISU_PW` | Visualisation password (required for secured commands like `nfc/startlearn`) |

### Session

Chosen per run — usually via CLI flags rather than env vars, because
these change per season / per operator / per batch.

| Flag | Env var (optional default) | Notes |
|---|---|---|
| `--code-touch <uuid>` | `LOXONE_CODE_TOUCH_UUID` | The NFC Code Touch reader to enrol on. Find with `--list`. |
| `--user-uuid <uuid>` | `LOXONE_TARGET_USER_UUID` | The Loxone user to attach tags to. Find with `--list-users`. |
| `--names <file>` | — | Text file, one roster name per line. Names go through the transform (see `docs/workflow.md`). |
| `--dry-run` | — | Capture tag IDs and journal them, but do NOT call `addusernfc`. No Loxone-side writes. |
| `--force` | — | Rebind cards that are already assigned to another user. Rare; use only when you know what you're doing. |
| `--yes` | — | Skip the interactive name-transformation confirmation prompt. Use in scripts. |

### Discovery

Non-enrolment modes for finding things:

| Flag | Notes |
|---|---|
| `--list` | List NfcCodeTouch readers visible to the service account, then exit. |
| `--list-users` | List Loxone users (name, admin flag, uuid), then exit. |
| `--user-filter <substring>` | Only with `--list-users`. Case-insensitive substring match on name or uuid. |

### Diagnostics

| Flag | Notes |
|---|---|
| `-v`, `--verbose` | Log every HTTP request and websocket frame. Useful when debugging a connection issue; noisy in normal use. |

## Environment variables

Full list, in one place, for reference. Set in `.env` (recommended) or
exported in the shell.

| Variable | Purpose |
|---|---|
| `LOXONE_HOSTS` | Comma-separated reader-owning Miniserver IPs (Trust cluster) |
| `LOXONE_HOST` | Legacy single-host form. `LOXONE_HOSTS` wins if both set. |
| `LOXONE_USER_HOST` | Miniserver where the target user lives. Defaults to first `LOXONE_HOSTS` entry. |
| `LOXONE_USER` | Service account name. Default `svc.cardenroll`. |
| `LOXONE_PW` | Service account login password |
| `LOXONE_VISU_PW` | Service account visualisation password |
| `LOXONE_CODE_TOUCH_UUID` | Default reader UUID (session scope — rarely worth setting) |
| `LOXONE_TARGET_USER_UUID` | Default target user UUID (session scope — rarely worth setting) |
| `LOXONE_ENV_FILE` | Path to the `.env` file to load. Default `.env` in CWD. |
| `AUTOLOX_DB` | Path to the SQLite journal. Default `./autolox.db`. |
| `AUTOLOX_CSV_EXPORT` | Optional. If set, every successful bind is also appended to this CSV path. |

## Typical workflows

### Discover then dry-run a small batch

```bash
python loxone_bulk_enroll.py --list-users --user-filter temp
# → shows temp.test, temp.xmas, etc.

python loxone_bulk_enroll.py --list
# → shows all readers visible to svc.cardenroll

echo -e "Jan Jansen\nAnne de Wit\nAnne-Marie Bakker" > names.txt

python loxone_bulk_enroll.py --dry-run \
    --code-touch 18b9fd41-.... \
    --user-uuid 211596fe-.... \
    --names names.txt
```

Walk to the reader, tap 3 cards, watch the output. Nothing is written to
Loxone; the tag IDs land in the local SQLite journal.

### Live enrolment

Drop `--dry-run`:

```bash
python loxone_bulk_enroll.py \
    --code-touch 18b9fd41-.... \
    --user-uuid 211596fe-.... \
    --names names.txt
```

The transformation preview appears — review the roster-form → loxone-form
mapping, confirm with `y`, then tap cards.

### Resume a partial session

If you stopped mid-batch (Ctrl-C, laptop closed, etc), re-run the same
command with the same roster. The tool cross-references our DB for tags
already bound to the target user; those cards are skipped on re-tap so
you never double-consume a name.

### Scripting (no interactive prompts)

```bash
python loxone_bulk_enroll.py --yes \
    --code-touch 18b9fd41-.... \
    --user-uuid 211596fe-.... \
    --names names.txt
```

`--yes` skips the transformation confirmation. Password prompts are
skipped automatically when `LOXONE_PW` and `LOXONE_VISU_PW` are set in
the environment.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including a completed enrolment or a graceful stop) |
| 1 | Reader not found / no readers visible |
| 2 | Missing required flag or bad usage |
| 3 | One or more roster rows have an unusable name (blank, all-symbols, etc.) — fix `names.txt` and retry |
| 130 | Interrupted by Ctrl-C (standard SIGINT exit code) |
