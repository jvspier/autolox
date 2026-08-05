# Docker deployment

The fastest way to run autolox on your own server, homelab, or NAS.

## Quick start

Prerequisites: Docker + Docker Compose plugin (any Docker Desktop or a
Linux host with the `docker.io` and `docker-compose-plugin` packages
from your distro).

```bash
git clone https://github.com/jvspier/autolox.git
cd autolox
cp .env.example .env
# edit .env with your Miniserver IPs, service account, passwords
docker compose up -d
```

Then open **http://your-docker-host:8000** and walk through Setup.

That's it. autolox is running, the SQLite DB persists across restarts
via a named volume (`autolox-data`), and the service will auto-start
whenever Docker starts.

## Configuration

All configuration comes from `.env` (in the compose directory) or plain
environment variables passed to Docker. Required values:

| Variable | Meaning |
|---|---|
| `LOXONE_HOSTS` | Comma-separated Miniserver IPs (Trust cluster) |
| `LOXONE_USER_HOST` | Miniserver where the target user lives (usually the "main" one) |
| `LOXONE_USER` | Service account name (default `svc.cardenroll`) |
| `LOXONE_PW` | Service account login password |
| `LOXONE_VISU_PW` | Service account visualisation password (for secured commands) |

`.env.example` in the repo root shows the format.

## Adding HTTPS

The autolox container speaks plain HTTP on port 8000, matching how most
self-hosted homelab tools work. If your setup already has a reverse
proxy (Nginx Proxy Manager, Traefik, Caddy, Cloudflare Tunnel, ...) —
just route your chosen hostname to `http://autolox-host:8000` and let
your existing proxy do TLS termination. No changes needed on the
autolox side.

**If you don't already have a reverse proxy**, the easiest option is
Caddy — one config line and automatic Let's Encrypt certificates on a
public hostname, or a self-signed cert for a purely internal one. Drop
this into a `Caddyfile` next to `docker-compose.yml`:

```
autolox.your.tld {
    reverse_proxy autolox:8000
}
```

And extend the compose file:

```yaml
services:
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    depends_on:
      - autolox

volumes:
  caddy-data: {}
  caddy-config: {}
```

Then `docker compose up -d`. Caddy handles TLS automatically for a
publicly-resolvable hostname; for LAN-only use, a self-signed cert
works with `tls internal` in the Caddyfile — accept the browser
warning once per device.

## Volumes and data

- `autolox-data` (named volume) — holds `autolox.db`. Bindings and
  session history survive container recreation.
- Optional: set `AUTOLOX_CSV_EXPORT=/data/enrolled.csv` in your `.env`
  to also write a CSV audit log inside the same volume.

To back up: `docker run --rm -v autolox-data:/data -v $(pwd):/backup
alpine tar czf /backup/autolox-backup.tgz -C /data .` (or any other
volume-backup tool you already use).

## Updating

```bash
git pull
docker compose build
docker compose up -d
```

The named volume persists your data across rebuilds. If a schema
migration is ever needed the app applies it automatically on startup
via `ALTER TABLE` — no manual step.

## Troubleshooting

- **Setup page can't reach Miniserver** — the container needs LAN
  reachability to your Miniservers. If Docker is bridged (default), it
  can reach the host's LAN; if it's on a custom Docker network, verify
  the network can talk to the Miniserver subnet.
- **Permissions error on volume** — the container runs as UID 1000. If
  you're using a bind mount instead of the named volume, `chown -R
  1000:1000 /path/to/bind`.
- **Port 8000 already in use** — change the host-side port in
  `docker-compose.yml`: `- "8080:8000"`.
