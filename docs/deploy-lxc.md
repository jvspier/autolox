# LXC deployment (Proxmox and similar)

Native install into a Debian/Ubuntu LXC container. No Docker layer.
Two paths — a one-shot script for speed, or manual steps for
transparency (and for the times when a script breaks and you need to
diagnose).

## Path A: one-shot install script

On a fresh Debian 12 / Ubuntu 22.04+ LXC (or the equivalent), as root:

```bash
curl -fsSL https://raw.githubusercontent.com/jvspier/autolox/main/deploy/lxc-install.sh | bash
```

That's it — the script installs everything, then prints next steps
(edit `/etc/autolox/env`, `systemctl start autolox`).

Environment variables you can set BEFORE running the script if you
want to override defaults:

| Variable | Default | Purpose |
|---|---|---|
| `AUTOLOX_REPO` | `https://github.com/jvspier/autolox.git` | Where to clone from |
| `AUTOLOX_BRANCH` | `main` | Branch/tag to install |
| `AUTOLOX_INSTALL_DIR` | `/opt/autolox` | Where the code lives |
| `AUTOLOX_DATA_DIR` | `/var/lib/autolox` | Where the SQLite DB lives |
| `AUTOLOX_CONFIG_DIR` | `/etc/autolox` | Where the env file lives |
| `AUTOLOX_USER` | `autolox` | System user the service runs as |

The script is **idempotent** — re-running it updates the code, refreshes
the venv, and reinstalls the systemd unit without disturbing your
`/etc/autolox/env` config.

## Path B: manual steps

Everything the script does, laid out — for when you'd rather see what's
happening or need to diagnose a failure.

### 1. Create the LXC

On the Proxmox host, create a Debian 12 (or Ubuntu LTS) unprivileged
LXC container. Typical resource shape:

- 1 vCPU
- 512 MB RAM
- 4 GB disk
- On a LAN bridge that can reach your Miniservers

Log in as root.

### 2. Install system packages

```bash
apt update
apt install -y --no-install-recommends \
    python3 python3-venv python3-pip git curl ca-certificates
```

### 3. Create the service user and directories

```bash
useradd --system --home /opt/autolox --shell /usr/sbin/nologin autolox
mkdir -p /opt/autolox /var/lib/autolox /etc/autolox
```

### 4. Clone the repo

```bash
git clone https://github.com/jvspier/autolox.git /opt/autolox
```

### 5. Create the Python venv and install

```bash
python3 -m venv /opt/autolox/.venv
/opt/autolox/.venv/bin/pip install --upgrade pip setuptools wheel
/opt/autolox/.venv/bin/pip install -e '/opt/autolox[web]'
```

### 6. Configure

Copy the example env file and edit it:

```bash
cp /opt/autolox/.env.example /etc/autolox/env
chmod 600 /etc/autolox/env
echo "AUTOLOX_DB=/var/lib/autolox/autolox.db" >> /etc/autolox/env
$EDITOR /etc/autolox/env   # fill in your real values
```

You need at minimum:

```
LOXONE_HOSTS=192.0.2.1,192.0.2.2,192.0.2.3,192.0.2.4
LOXONE_USER_HOST=192.0.2.1
LOXONE_USER=svc.cardenroll
LOXONE_PW=<login password>
LOXONE_VISU_PW=<visu password>
AUTOLOX_DB=/var/lib/autolox/autolox.db
```

### 7. Fix ownership

```bash
chown -R autolox:autolox /opt/autolox /var/lib/autolox
chown root:autolox /etc/autolox/env
chmod 640 /etc/autolox/env
```

### 8. Install the systemd unit

```bash
install -m 644 /opt/autolox/deploy/autolox.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now autolox
```

### 9. Verify

```bash
systemctl status autolox
journalctl -u autolox -f
```

Then hit `http://<lxc-ip>:8000` from any browser on the same LAN.

## Updating

Whichever path you used:

```bash
cd /opt/autolox
sudo git pull
sudo /opt/autolox/.venv/bin/pip install -e '/opt/autolox[web]'
sudo systemctl restart autolox
```

Or just re-run `lxc-install.sh` — it's idempotent.

## HTTPS (optional)

autolox itself only speaks plain HTTP on port 8000. This is fine on a
trusted LAN with a service account whose creds live in a chmod-640
file. If you want HTTPS anyway (typical if colleagues will bookmark it
under a friendly hostname):

- Run a reverse proxy on the same LXC or a shared homelab LXC that
  handles TLS for other services already.
- Caddy is the shortest path — a two-line `Caddyfile` per site,
  automatic certificates via Let's Encrypt (public hostname) or
  self-signed (`tls internal`).

Example — install Caddy on the same LXC:

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    tee /etc/apt/trusted.gpg.d/caddy-stable.asc
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
    tee /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install caddy
```

Then `/etc/caddy/Caddyfile`:

```
autolox.your.tld {
    reverse_proxy localhost:8000
}
```

Restart Caddy and you're done.

## Backup

The SQLite journal is the only thing worth backing up:

```bash
cp /var/lib/autolox/autolox.db /some/backup/location/autolox-$(date +%F).db
```

Or set up a cron / systemd timer to snapshot it weekly. Small file,
compresses well.

## Uninstall

```bash
sudo systemctl disable --now autolox
sudo rm /etc/systemd/system/autolox.service
sudo systemctl daemon-reload
sudo rm -rf /opt/autolox /var/lib/autolox /etc/autolox
sudo userdel autolox
```
