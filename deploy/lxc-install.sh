#!/usr/bin/env bash
# autolox — one-shot installer for a fresh Debian/Ubuntu LXC.
#
# Idempotent: safe to re-run to update or repair an install.
#
# What it does:
#   - installs the system packages needed (python3, venv, git, curl)
#   - clones the repo to /opt/autolox (or updates it if already there)
#   - creates a Python venv and pip-installs the app + web extras
#   - creates the `autolox` system user
#   - creates /var/lib/autolox (SQLite lives here, owned by autolox)
#   - creates /etc/autolox/env from .env.example on first run
#   - installs and enables the systemd unit
#
# It does NOT start the service on first run — you must edit
# /etc/autolox/env with your real values first.
#
# Usage (on a fresh LXC as root):
#   curl -fsSL https://raw.githubusercontent.com/jvspier/autolox/main/deploy/lxc-install.sh | bash
# or, cloned locally:
#   sudo bash deploy/lxc-install.sh

set -euo pipefail

REPO="${AUTOLOX_REPO:-https://github.com/jvspier/autolox.git}"
BRANCH="${AUTOLOX_BRANCH:-main}"
INSTALL_DIR="${AUTOLOX_INSTALL_DIR:-/opt/autolox}"
DATA_DIR="${AUTOLOX_DATA_DIR:-/var/lib/autolox}"
CONFIG_DIR="${AUTOLOX_CONFIG_DIR:-/etc/autolox}"
SERVICE_USER="${AUTOLOX_USER:-autolox}"

if [[ $EUID -ne 0 ]]; then
    echo "error: run as root (sudo bash $0)" >&2
    exit 1
fi

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip git curl ca-certificates

say "creating service user + directories"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$CONFIG_DIR"

say "cloning / updating repo at $INSTALL_DIR"
# git 2.35+ refuses to operate on a repo whose .git dir isn't owned by
# the current user. During updates the checkout is autolox-owned but
# we're running as root, so we tell git this specific path is safe.
# `-c safe.directory=` is scoped per-invocation, doesn't pollute the
# persistent config.
GIT_SAFE=(-c "safe.directory=$INSTALL_DIR")
if [[ -d "$INSTALL_DIR/.git" ]]; then
    git "${GIT_SAFE[@]}" -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git "${GIT_SAFE[@]}" -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
    git "${GIT_SAFE[@]}" clone --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi

say "installing Python dependencies into venv"
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip setuptools wheel
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"'[web]'

say "seeding config at $CONFIG_DIR/env (if missing)"
if [[ ! -f "$CONFIG_DIR/env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$CONFIG_DIR/env"
    chmod 600 "$CONFIG_DIR/env"
    # systemd EnvironmentFile= expects no shell-style comments prefixed
    # by whitespace; .env.example is compatible but we tell the operator
    # to strip anything they don't need.
    say "  edit $CONFIG_DIR/env with your Miniserver IPs and credentials"
fi

say "point AUTOLOX_DB at persistent storage"
if ! grep -q '^AUTOLOX_DB=' "$CONFIG_DIR/env"; then
    printf '\n# location for the SQLite journal (managed by systemd)\nAUTOLOX_DB=%s/autolox.db\n' "$DATA_DIR" >> "$CONFIG_DIR/env"
fi

say "fixing ownership"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"
chown root:"$SERVICE_USER" "$CONFIG_DIR/env"
chmod 640 "$CONFIG_DIR/env"

say "installing systemd unit"
install -m 644 "$INSTALL_DIR/deploy/autolox.service" /etc/systemd/system/autolox.service
systemctl daemon-reload
systemctl enable autolox.service

# If the service was already running (i.e. this is an update, not a
# fresh install), restart it so it picks up the new code.
if systemctl is-active --quiet autolox.service; then
    say "restarting autolox (service was running)"
    systemctl restart autolox.service
fi

cat <<EOF

$(tput bold 2>/dev/null || true)Install complete.$(tput sgr0 2>/dev/null || true)

Next steps:
  1. Edit $CONFIG_DIR/env with your real values:
       LOXONE_HOSTS=10.x.x.1,10.x.x.2,...
       LOXONE_USER_HOST=10.x.x.1
       LOXONE_USER=svc.cardenroll
       LOXONE_PW=...
       LOXONE_VISU_PW=...
  2. Start the service:
       systemctl start autolox
  3. Watch the logs:
       journalctl -u autolox -f
  4. Open http://<this-lxc-ip>:8000 in a browser.

To update later: re-run this script. It's idempotent.

EOF
