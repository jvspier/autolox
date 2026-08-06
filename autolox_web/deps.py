"""FastAPI dependencies — LoxoneClient lifetime, Store lifetime, config loading.

The client + store are created at app startup and shared across requests.
Iteration 1 is single-process single-operator; if we ever grow beyond
that, revisit thread/connection sharing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class WebConfig:
    reader_hosts: list[str]
    user_host: str | None
    user: str
    password: str
    visu_password: str


def load_config(env_file: str | None = None) -> WebConfig:
    """Load configuration from .env / environment. Raises RuntimeError with
    a specific missing-field message if anything required is absent.

    Prefers `LOXONE_HOSTS` (comma-separated list) for the reader hosts.
    Falls back to `LOXONE_HOST` (single). This lets a Trust cluster's
    per-Miniserver reader lists all be enumerated.
    """
    load_dotenv(env_file or os.environ.get("LOXONE_ENV_FILE", ".env"),
                override=False)

    missing: list[str] = []
    def _req(name: str) -> str:
        val = os.environ.get(name, "").strip()
        if not val:
            missing.append(name)
        return val

    hosts_env = os.environ.get("LOXONE_HOSTS", "").strip()
    if hosts_env:
        reader_hosts = [h.strip() for h in hosts_env.split(",") if h.strip()]
    else:
        single = _req("LOXONE_HOST")
        reader_hosts = [single] if single else []

    user = os.environ.get("LOXONE_USER", "svc.cardenroll").strip()
    password = _req("LOXONE_PW")
    visu = _req("LOXONE_VISU_PW")
    user_host = (os.environ.get("LOXONE_USER_HOST") or "").strip() or None

    if not reader_hosts:
        missing.append("LOXONE_HOSTS or LOXONE_HOST")
    if missing:
        raise RuntimeError(
            "missing required env vars: " + ", ".join(missing) +
            " (set them in .env or the shell environment before starting)")

    return WebConfig(reader_hosts=reader_hosts, user_host=user_host,
                     user=user, password=password, visu_password=visu)


def load_web_auth(env_file: str | None = None) -> tuple[str, str | None]:
    """Basic-auth credentials for the web UI itself - separate from the
    Loxone service-account credentials in WebConfig. Password is optional:
    if unset, the web UI runs with no authentication (the original iteration-1
    tradeoff, documented in docs/phase-3-spec.md) rather than refusing to
    start, so existing deployments aren't broken by upgrading in place."""
    load_dotenv(env_file or os.environ.get("LOXONE_ENV_FILE", ".env"),
                override=False)
    user = os.environ.get("AUTOLOX_WEB_USER", "").strip() or "admin"
    password = os.environ.get("AUTOLOX_WEB_PASSWORD", "").strip() or None
    return user, password
