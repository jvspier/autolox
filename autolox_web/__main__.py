"""Convenience launcher — `python -m autolox_web`.

Starts uvicorn on 127.0.0.1:8000 (by default) and opens the default
browser to that URL. Handy for anyone who cloned the repo and wants to
run autolox without Docker or a systemd unit — just one command and a
browser tab.

For scripted / production use, prefer:

    uvicorn autolox_web.app:app --host 0.0.0.0 --port 8000

directly, since that's what the Dockerfile and the systemd unit do.

Flags:

    --host        listen address (default 127.0.0.1)
    --port        listen port (default 8000)
    --no-browser  don't open a browser tab (e.g., on a headless machine)
    --reload      hot-reload on code changes (dev only)

Exit with Ctrl-C.
"""
from __future__ import annotations

import argparse
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m autolox_web",
        description="Run the autolox web app locally + open a browser tab.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default 127.0.0.1). Use 0.0.0.0 "
                             "to accept connections from other machines on "
                             "your LAN — but consider a reverse proxy for "
                             "TLS in that case.")
    parser.add_argument("--port", type=int, default=8000,
                        help="Bind port (default 8000).")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't try to open a browser tab. Useful on a "
                             "headless machine or in a container.")
    parser.add_argument("--reload", action="store_true",
                        help="Enable uvicorn's hot-reload. Dev-only.")
    args = parser.parse_args()

    # Uvicorn imports pull in a fair amount of async machinery; defer
    # them so `--help` stays quick.
    import uvicorn

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}"

    if not args.no_browser:
        # Wait a moment for uvicorn to bind the port, then open. Running
        # this on a timer thread keeps the main thread free for uvicorn.
        # If the open fails (headless / no browser installed) we just
        # log and keep serving.
        def _open() -> None:
            try:
                webbrowser.open(url, new=2)
            except Exception:
                print(f"[autolox-web] could not open a browser — visit {url}")
        threading.Timer(1.0, _open).start()

    print(f"[autolox-web] serving on {url}")
    print(f"[autolox-web] Ctrl-C to stop")
    uvicorn.run(
        "autolox_web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
