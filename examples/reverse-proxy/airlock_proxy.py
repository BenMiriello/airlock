"""Reverse proxy in front of ComfyUI / Forge — acquires an airlock lease
before forwarding generation requests, releases when the upstream completes.

Use this when the drop-in extensions (examples/comfyui-airlock/,
examples/airlock-forge/) aren't an option — e.g., you don't control the
ComfyUI/Forge install but you do control the port routing.

Wraps either backend; pick which one with --backend.

Stdlib only (urllib + http.server) for the same reason the daemon is:
zero deps, drops onto any host without venv setup.

Caveats:
  - Latency: ~5-10 ms per request for the lease round-trip + body forwarding.
  - WebSocket: NOT proxied in this minimal version. ComfyUI's /ws stream
    (progress events) won't tunnel. Clients that rely on /ws for progress
    will lose live updates but generations still complete; the proxy
    polls /history for completion. To proxy /ws too, use the aiohttp-based
    variant (left as a future enhancement, ~150 LOC).
  - The proxy releases on completion timeout (default 10 min). Set
    --max-wait higher for slow workflows.

Usage:
    python3 airlock_proxy.py \\
        --backend comfyui \\
        --upstream http://127.0.0.1:8188 \\
        --listen 0.0.0.0:8189 \\
        --app comfyui \\
        --budget 14g \\
        --priority 60

    # Forge:
    python3 airlock_proxy.py \\
        --backend forge \\
        --upstream http://127.0.0.1:7860 \\
        --listen 0.0.0.0:7861 \\
        --app forge --budget 10g

After starting, point your client at the proxy port (8189 / 7861).
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
import logging
import os
import re
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


log = logging.getLogger("airlock_proxy")


# ---------- airlock client (fail-open) ----------


def _airlock_post(airlock_url: str, path: str, body: dict, timeout: float = 60.0) -> dict | None:
    try:
        req = urllib.request.Request(
            airlock_url.rstrip("/") + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _airlock_delete(airlock_url: str, path: str, timeout: float = 5.0) -> None:
    try:
        req = urllib.request.Request(airlock_url.rstrip("/") + path, method="DELETE")
        urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, OSError):
        pass


# ---------- proxy core ----------


class Config:
    backend: str
    upstream_host: str
    upstream_port: int
    airlock_url: str
    app: str
    budget_mib: int
    priority: int
    max_wait_s: int


CFG = Config()


# Path that triggers a lease for each backend.
TRIGGER_PATHS = {
    "comfyui": [re.compile(r"^/prompt$")],
    "forge": [re.compile(r"^/sdapi/v1/txt2img$"),
              re.compile(r"^/sdapi/v1/img2img$")],
}


def _is_trigger(path: str) -> bool:
    path_no_qs = path.split("?", 1)[0]
    return any(rx.match(path_no_qs) for rx in TRIGGER_PATHS.get(CFG.backend, []))


def _wait_for_prompt_done(prompt_id: str) -> None:
    """ComfyUI: poll /history/<id> until the prompt shows up there."""
    deadline = time.monotonic() + CFG.max_wait_s
    while time.monotonic() < deadline:
        try:
            r = http.client.HTTPConnection(CFG.upstream_host, CFG.upstream_port, timeout=5)
            r.request("GET", f"/history/{prompt_id}")
            resp = r.getresponse()
            body = resp.read()
            r.close()
            if resp.status == 200:
                data = json.loads(body or b"{}")
                if prompt_id in data:
                    return
        except (http.client.HTTPException, OSError, ValueError):
            pass
        time.sleep(1.0)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet by default; flip to log.info(...) for verbose debug.
        return

    def _forward(self, lease_id: str | None = None) -> None:
        """Forward this request to the upstream; copy response back to client.

        If lease_id is set and this is a trigger path, fire-and-forget a
        background thread that waits for completion and releases the lease.
        """
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else None

        try:
            conn = http.client.HTTPConnection(CFG.upstream_host, CFG.upstream_port, timeout=600)
            # Strip Host header so upstream sees its own.
            fwd_headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
            conn.request(self.command, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "content-length", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()
        except (http.client.HTTPException, OSError) as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"upstream error: {e}".encode())
            if lease_id:
                _airlock_delete(CFG.airlock_url, f"/lease/{lease_id}")
            return

        # If trigger, spawn release-on-completion watcher.
        if lease_id and CFG.backend == "comfyui" and resp.status == 200:
            try:
                prompt_id = json.loads(resp_body).get("prompt_id")
            except (ValueError, KeyError):
                prompt_id = None
            if prompt_id:
                threading.Thread(
                    target=_watch_comfyui, args=(lease_id, prompt_id),
                    daemon=True, name="release-watcher",
                ).start()
            else:
                _airlock_delete(CFG.airlock_url, f"/lease/{lease_id}")
        elif lease_id and CFG.backend == "forge":
            # Forge's txt2img/img2img are blocking — by the time we returned
            # to the client the generation is done. Safe to release now.
            _airlock_delete(CFG.airlock_url, f"/lease/{lease_id}")

    def do_GET(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        if not _is_trigger(self.path):
            self._forward()
            return
        body = {
            "app": CFG.app,
            "vram_budget_mib": CFG.budget_mib,
            "priority": CFG.priority,
            "client_pid": os.getpid(),
            "reason": f"{CFG.backend} {self.path}",
            "blocking": True,
            "wait_timeout_s": CFG.max_wait_s,
        }
        lease = _airlock_post(CFG.airlock_url, "/lease", body, timeout=CFG.max_wait_s + 5)
        lease_id = lease.get("id") if isinstance(lease, dict) else None
        if lease_id:
            log.info("acquired lease %s for %s", lease_id, self.path)
        else:
            log.info("airlock unavailable — forwarding without lease")
        self._forward(lease_id=lease_id)

    def do_OPTIONS(self) -> None:
        self._forward()


def _watch_comfyui(lease_id: str, prompt_id: str) -> None:
    _wait_for_prompt_done(prompt_id)
    _airlock_delete(CFG.airlock_url, f"/lease/{lease_id}")
    log.info("released lease %s after prompt %s", lease_id, prompt_id[:8])


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _parse_budget(s: str) -> int:
    s = s.lower().strip()
    m = re.match(r"^([0-9.]+)\s*(mib|m|gib|g|mb|gb)?$", s)
    if not m:
        raise ValueError(f"bad budget: {s!r}")
    val = float(m.group(1))
    unit = m.group(2) or "mib"
    return int(val * 1024) if unit in ("g", "gb", "gib") else int(val)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", choices=["comfyui", "forge"], required=True)
    ap.add_argument("--upstream", required=True,
                    help="upstream URL e.g. http://127.0.0.1:8188")
    ap.add_argument("--listen", default="0.0.0.0:8189",
                    help="listen address host:port")
    ap.add_argument("--airlock-url", default=os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8447"))
    ap.add_argument("--app", required=True,
                    help="airlock app name to register leases under")
    ap.add_argument("--budget", required=True, help="VRAM budget per generation, e.g. 14g")
    ap.add_argument("--priority", type=int, default=60)
    ap.add_argument("--max-wait-s", type=int, default=600,
                    help="max time to wait for lease + completion")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    up = urllib.parse.urlparse(args.upstream)
    CFG.backend = args.backend
    CFG.upstream_host = up.hostname or "127.0.0.1"
    CFG.upstream_port = up.port or (8188 if args.backend == "comfyui" else 7860)
    CFG.airlock_url = args.airlock_url
    CFG.app = args.app
    CFG.budget_mib = _parse_budget(args.budget)
    CFG.priority = args.priority
    CFG.max_wait_s = args.max_wait_s

    host, _, port = args.listen.partition(":")
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port or "8189")), ProxyHandler)
    log.info("airlock proxy %s → %s:%s; app=%s budget=%dMiB",
             args.listen, CFG.upstream_host, CFG.upstream_port, CFG.app, CFG.budget_mib)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
