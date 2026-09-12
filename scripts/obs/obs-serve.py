#!/usr/bin/python3.12
"""obs-serve.py — OBJ-27 F5d: the stable URL of the hyperespace.

A tiny stdlib HTTP server that serves the F5c portal at
http://localhost:8917 (configurable via --port). It owns the PORTAL LIFECYCLE:

  - default mode: the portal is regenerated in the background every
    --refresh minutes AND immediately when obs/trace.jsonl changes
    (on-change), then served as static files — a request never triggers a
    rebuild, so the page is always fast and the server never blocks on the
    board DB.
  - --live: legacy OBJ-32 behavior, regenerate on every request (useful
    for adoptants without a cron tick).

PRIVACY: binds 127.0.0.1 ONLY — nothing leaves the host. The hiperespacio
es de la casa y en la casa.

RESPAWN CONTRACT (F5d): this process is a long-lived daemon; the governor's
tick respawns it via obs-serve-cron.sh when the port is dead (same daemon
pattern as the kanban daemon: PID file, no double-start, silent when
healthy). --check is that probe: exit 0 + silence when alive, exit 1 +
diagnostic when dead; the tick only acts on the failure. --stop shuts it
down cleanly. --headless never binds: it regenerates the static portal and
exits (adoptants without a resident server).

Zero tokens, no network beyond localhost, stdlib only. Exit 0 always for
supervisory modes; the server itself runs until SIGTERM.
"""
from __future__ import annotations

import argparse
import http.server
import importlib.util
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit(f"obs-serve: cannot load sibling {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pb = _load("portal_build", _HERE / "portal-build.py")
ms = pb.ms  # morning-screen via portal-build (single source of truth)

DEFAULT_PORT = 8917
DEFAULT_REFRESH_MIN = 5.0
PID_NAME = "obs-serve.pid"
LOG_NAME = "obs-serve.log"


def pid_path(hermes_home=None) -> Path:
    return pb.od.dashboard_path(hermes_home).parent / PID_NAME


def log_path(hermes_home=None) -> Path:
    return pb.od.dashboard_path(hermes_home).parent / LOG_NAME


def _log(msg: str, hermes_home=None) -> None:
    try:
        p = log_path(hermes_home)
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Health / lifecycle helpers (used by --check, the cron respawn and --stop)
# ---------------------------------------------------------------------------

def port_alive(port: int, timeout: float = 1.0) -> bool:
    """True when something answers on 127.0.0.1:port (the URL is live)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def read_pid(hermes_home=None):
    try:
        return int(pid_path(hermes_home).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    """True when the PID exists (0-signal probe)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def stop_server(hermes_home=None) -> bool:
    """SIGTERM the recorded server (if any); True when it was running."""
    pid = read_pid(hermes_home)
    if pid and pid_alive(pid):
        try:
            os.kill(int(pid), getattr(signal, "SIGTERM", 15))
            _log(f"stopped by --stop (pid {pid})", hermes_home)
            return True
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# Regeneration loop: every N minutes + on-change of the trace
# ---------------------------------------------------------------------------

def _trace_stamp(hermes_home=None) -> tuple:
    try:
        st = ms.trace_path(hermes_home).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


class Regenerator:
    """Background regeneration: timer + trace on-change. Daemon thread."""

    def __init__(self, hermes_home=None, refresh_min: float =
                 DEFAULT_REFRESH_MIN):
        self.home = hermes_home
        self.refresh_s = max(30.0, float(refresh_min) * 60.0)
        self.last_stamp = _trace_stamp(hermes_home)
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()

    def regenerate(self, reason: str) -> None:
        with self.lock:  # one rebuild at a time
            try:
                target = pb.write_portal(hermes_home=self.home)
                _log(f"regenerated ({reason}) -> {target}", self.home)
            except Exception as exc:  # never kill the server for a rebuild
                _log(f"regenerate error ({reason}): {exc!r}", self.home)

    def loop(self) -> None:
        while not self.stop_flag.wait(self.refresh_s):
            stamp = _trace_stamp(self.home)
            if stamp != self.last_stamp:
                self.last_stamp = stamp
                self.regenerate("on-change")
            else:
                self.regenerate("periodic")

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, daemon=True)
        th.start()
        return th

    def run(self) -> None:
        self.regenerate("boot")
        self.loop()


# ---------------------------------------------------------------------------
# Server: static files from the portal dir (+ live mode for adoptants)
# ---------------------------------------------------------------------------

def _content_type(name: str) -> str:
    if name.endswith(".html"):
        return "text/html; charset=utf-8"
    if name.endswith(".json"):
        return "application/json; charset=utf-8"
    return "application/octet-stream"


def make_server(port: int, hermes_home=None, live: bool = False):
    """Static portal server (default) or regenerate-on-request (--live)."""
    pdir = pb.portal_dir(hermes_home)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split("?", 1)[0].strip("/")
            name = route or "index"
            if name in ("", "index.html"):
                name = "index.html"
            elif not name.endswith(".html"):
                name = f"{name}.html"
            if "/" in name or name not in \
                    {f"{p}.html" for p in pb.PAGES}:
                self.send_error(404)
                return
            if live:
                try:
                    pages = pb.portal_build(hermes_home=hermes_home)
                    body = pages.get(name.removesuffix(".html"))
                except Exception as exc:  # never leak a stack to the page
                    body = ("<!doctype html><meta charset=utf-8><pre>error "
                            f"generando: {pb._esc(repr(exc))}</pre>")
            else:
                path = pdir / name
                try:
                    body = path.read_text(encoding="utf-8")
                except OSError:
                    # portal not generated yet: build once on demand
                    try:
                        pb.write_portal(hermes_home=hermes_home)
                        body = path.read_text(encoding="utf-8")
                    except Exception as exc:
                        body = ("<!doctype html><meta charset=utf-8>"
                                "<pre>portal no disponible: "
                                f"{pb._esc(repr(exc))}</pre>")
            data = (body or "").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", _content_type(name))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002
            pass  # quiet: a portal shouldn't spam

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"bind 127.0.0.1:{DEFAULT_PORT} (default)")
    ap.add_argument("--refresh", type=float, default=DEFAULT_REFRESH_MIN,
                    metavar="MIN",
                    help="regenerate the portal every N minutes "
                         f"(default {DEFAULT_REFRESH_MIN})")
    ap.add_argument("--live", action="store_true",
                    help="regenerate on every request instead of the "
                         "background loop")
    ap.add_argument("--headless", action="store_true",
                    help="no server: regenerate the static portal and exit "
                         "(adoptants)")
    ap.add_argument("--out", default=None,
                    help="with --headless: write the static portal here "
                         "instead of the default obs/portal/")
    ap.add_argument("--check", action="store_true",
                    help="supervisor probe: exit 0 when the URL is alive, "
                         "1 when dead (the tick's respawn trigger)")
    ap.add_argument("--stop", action="store_true",
                    help="stop a running server")
    args = ap.parse_args(argv)

    if args.check:
        if port_alive(args.port):
            return 0
        pid = read_pid()
        print(f"obs-serve: DOWN (port {args.port}, pid={pid})",
              file=sys.stderr)
        return 1
    if args.stop:
        ok = stop_server()
        print("obs-serve: stopped" if ok else "obs-serve: not running")
        return 0
    if args.headless:
        target = pb.write_portal(out=args.out)
        print(f"obs-serve: static portal at {target}")
        return 0

    if port_alive(args.port):  # never double-bind (idempotent respawn)
        print(f"obs-serve: port {args.port} already serving", file=sys.stderr)
        return 0
    try:
        srv = make_server(args.port, live=args.live)
    except OSError as exc:
        print(f"obs-serve: cannot bind {args.port}: {exc}", file=sys.stderr)
        return 1
    try:
        pid_path().parent.mkdir(parents=True, exist_ok=True)
        pid_path().write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    reg = None
    if not args.live:
        reg = Regenerator(refresh_min=args.refresh)
        reg.start()
    port = srv.server_address[1]
    print(f"hiperespacio: http://localhost:{port}  (Ctrl-C para salir)")
    _log(f"serving on 127.0.0.1:{port} (refresh={args.refresh}min, "
         f"live={args.live})")

    def _term(signum, frame):  # graceful: --stop / systemd-style SIGTERM
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        srv.server_close()
        if reg:
            reg.stop_flag.set()
        try:
            pid_path().unlink()
        except OSError:
            pass
        _log("server closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())