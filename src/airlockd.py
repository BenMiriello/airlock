"""airlockd — host-level GPU lease broker daemon.

Single-file daemon. Stdlib only.

Architecture:
  - The Broker (core.py) is the pure state machine, guarded by a lock.
  - A poller thread samples nvidia-smi every POLL_INTERVAL_S, updates lease
    actuals, surfaces unmanaged processes, creates implicit leases for
    matching cmdlines.
  - A watchdog thread checks PIDs of active leases + queued requests; dead
    PIDs auto-release.
  - An HTTP server thread serves the API on 127.0.0.1:8447.
  - State is persisted to disk on every mutation.
"""
from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow `python airlockd.py` from src/ to import siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
from core import Broker, GrantResult, Lease, Mode, PreemptHandler, Request, AppConfig
import gpu_query
from store import Store, AppRegistry


VERSION = "0.1.0"

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8447

POLL_INTERVAL_S = 2.0
WATCHDOG_INTERVAL_S = 2.0
TICK_INTERVAL_S = 1.0
PREEMPT_GRACE_S = 30
PREEMPT_KILL_GRACE_S = 10
# How often the tick loop runs the implicit-budget decay pass. Once a minute
# is enough; the decay function is cheap but logs an event per change.
BUDGET_DECAY_INTERVAL_S = 60


log = logging.getLogger("airlockd")


# ============================================================
# Runtime — broker + lock + supporting threads
# ============================================================


class Runtime:
    def __init__(self, broker: Broker, store: Store, app_registry: AppRegistry,
                 total_vram_override: int | None = None):
        self.broker = broker
        self.store = store
        self.app_registry = app_registry
        self.lock = threading.RLock()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._stop_flag = threading.Event()
        # PIDs we've already kicked off preemption escalation for, to avoid
        # double-SIGTERM. lease_id -> (preempt_started_mono, escalations_fired)
        self._preempt_state: dict[str, dict] = {}
        self._total_vram_override = total_vram_override

    # ------- lifecycle -------

    def start(self) -> None:
        # Hydrate broker from persisted state.
        snap = self.store.load_state()
        if snap:
            with self.lock:
                self.broker.restore(snap)
            log.info("restored state: %d active leases, %d queued, mode=%s",
                     len(self.broker.active_leases), len(self.broker.queue), self.broker.mode.value)
        # Load app registry.
        with self.lock:
            for name, d in self.app_registry.load_all().items():
                try:
                    self.broker.apps[name] = AppConfig.from_dict(d)
                except Exception as e:
                    log.warning("bad app registry entry %s: %s", name, e)
        # Pick up GPU total if not overridden.
        if self._total_vram_override:
            self.broker.total_vram_mib = self._total_vram_override
        else:
            snap_gpu = gpu_query.gpu_snapshot()
            if snap_gpu.available and snap_gpu.total_mib > 0:
                self.broker.total_vram_mib = snap_gpu.total_mib
                log.info("detected GPU total VRAM: %d MiB", snap_gpu.total_mib)
            else:
                log.warning("no GPU detected; using configured total=%d MiB",
                            self.broker.total_vram_mib)
        # Revalidate active-lease PIDs (after restart).
        self._revalidate_pids()
        # Start background threads.
        threading.Thread(target=self._poll_loop, daemon=True, name="gpu-poller").start()
        threading.Thread(target=self._watchdog_loop, daemon=True, name="pid-watchdog").start()
        threading.Thread(target=self._tick_loop, daemon=True, name="tick").start()
        threading.Thread(target=self._preempt_loop, daemon=True, name="preempt-escalator").start()
        self.store.log_event("daemon_start", version=VERSION, mode=self.broker.mode.value)

    def stop(self) -> None:
        self._stop_flag.set()
        self.store.log_event("daemon_stop")
        with self.lock:
            self._save_state_locked()

    # ------- state save -------

    def _save_state_locked(self) -> None:
        try:
            self.store.save_state(self.broker.snapshot())
        except Exception as e:
            log.error("save_state failed: %s", e)

    # ------- threads -------

    def _poll_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                procs = gpu_query.compute_apps()
                with self.lock:
                    known_pids = {l.client_pid for l in self.broker.active_leases.values()}
                    seen_pids = set()
                    for p in procs:
                        seen_pids.add(p.pid)
                        if p.pid in known_pids:
                            self.broker.update_actual_usage(p.pid, p.used_mib)
                            continue
                        # Always re-evaluate against current app registry so a
                        # newly-registered app can claim previously-unmanaged
                        # processes.
                        cmdline = (self.broker.unmanaged[p.pid].cmdline
                                   if p.pid in self.broker.unmanaged
                                   else gpu_query.read_cmdline(p.pid))
                        matched = self._match_app(cmdline)
                        if matched:
                            lease = self.broker.submit_implicit(
                                app=matched, client_pid=p.pid,
                                observed_vram_mib=p.used_mib, cmdline=cmdline,
                            )
                            if lease:
                                self.broker.remove_unmanaged(p.pid)
                                self.store.log_event(
                                    "implicit_lease",
                                    lease_id=lease.id, app=matched, pid=p.pid,
                                    cmdline=cmdline[:200], vram_mib=p.used_mib,
                                )
                                self._save_state_locked()
                        else:
                            self.broker.see_unmanaged(p.pid, cmdline, p.used_mib)
                    # Cull unmanaged records whose PID isn't in the latest snapshot.
                    for pid in list(self.broker.unmanaged):
                        if pid not in seen_pids:
                            self.broker.remove_unmanaged(pid)
            except Exception as e:
                log.exception("poll loop: %s", e)
            self._stop_flag.wait(POLL_INTERVAL_S)

    def _match_app(self, cmdline: str) -> str | None:
        if not cmdline:
            return None
        for name, app in self.broker.apps.items():
            if not app.cmdline_match:
                continue
            try:
                if re.search(app.cmdline_match, cmdline):
                    return name
            except re.error:
                continue
        return None

    def _watchdog_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                with self.lock:
                    dead_lease_ids: list[str] = []
                    for lease_id, l in list(self.broker.active_leases.items()):
                        if l.client_pid and not core._pid_alive_or_none(l.client_pid):
                            dead_lease_ids.append(lease_id)
                    for lid in dead_lease_ids:
                        self.broker.release(lid)
                        self.store.log_event("auto_release_dead_pid", lease_id=lid)
                    # Same for queue: drop queued requests whose client PID is gone.
                    keep: list[Request] = []
                    for r in self.broker.queue:
                        if r.client_pid and not core._pid_alive_or_none(r.client_pid):
                            self.store.log_event("queue_drop_dead_pid", req_id=r.id, app=r.app)
                            continue
                        keep.append(r)
                    self.broker.queue = keep
                    if dead_lease_ids or len(keep) != len(self.broker.queue):
                        self._save_state_locked()
            except Exception as e:
                log.exception("watchdog: %s", e)
            self._stop_flag.wait(WATCHDOG_INTERVAL_S)

    def _tick_loop(self) -> None:
        decay_counter = 0
        while not self._stop_flag.is_set():
            try:
                with self.lock:
                    effect = self.broker.tick()
                    if effect.grants or effect.timeouts:
                        for l in effect.grants:
                            self.store.log_event("grant", lease_id=l.id, app=l.app,
                                                 budget_mib=l.vram_budget_mib, priority=l.priority)
                        for r in effect.timeouts:
                            self.store.log_event("timeout", req_id=r.id, app=r.app)
                        self._save_state_locked()
                    # Implicit-lease budget decay every BUDGET_DECAY_INTERVAL ticks.
                    decay_counter += 1
                    if decay_counter * TICK_INTERVAL_S >= BUDGET_DECAY_INTERVAL_S:
                        decay_counter = 0
                        changes = self.broker.decay_implicit_budgets()
                        for lid, old, new in changes:
                            self.store.log_event("implicit_budget_decay",
                                                 lease_id=lid, old_mib=old, new_mib=new)
                        if changes:
                            self._save_state_locked()
            except Exception as e:
                log.exception("tick: %s", e)
            self._stop_flag.wait(TICK_INTERVAL_S)

    def _preempt_loop(self) -> None:
        """Watch PREEMPTING leases, escalate to SIGTERM/SIGKILL after grace."""
        while not self._stop_flag.is_set():
            try:
                now = time.monotonic()
                with self.lock:
                    for lease_id, l in list(self.broker.active_leases.items()):
                        if l.state != core.LeaseState.PREEMPTING.value:
                            continue
                        st = self._preempt_state.setdefault(lease_id, {
                            "started": now, "sigterm_sent": False, "sigkill_sent": False,
                        })
                        age = now - st["started"]
                        if age >= PREEMPT_GRACE_S and not st["sigterm_sent"]:
                            self._sigterm(l)
                            st["sigterm_sent"] = True
                        elif age >= PREEMPT_GRACE_S + PREEMPT_KILL_GRACE_S and not st["sigkill_sent"]:
                            self._sigkill(l)
                            st["sigkill_sent"] = True
                            # Force-release; the next poll will see the VRAM gone.
                            self.broker.release(lease_id)
                            self.store.log_event("force_release_after_sigkill", lease_id=lease_id)
                            self._save_state_locked()
                    # Clean tracker for leases that are no longer preempting.
                    for lid in list(self._preempt_state):
                        if lid not in self.broker.active_leases:
                            del self._preempt_state[lid]
                        elif self.broker.active_leases[lid].state != core.LeaseState.PREEMPTING.value:
                            del self._preempt_state[lid]
            except Exception as e:
                log.exception("preempt loop: %s", e)
            self._stop_flag.wait(1.0)

    def _sigterm(self, l: Lease) -> None:
        if not l.client_pid:
            return
        try:
            os.kill(l.client_pid, signal.SIGTERM)
            self.store.log_event("preempt_sigterm", lease_id=l.id, pid=l.client_pid, app=l.app)
        except (ProcessLookupError, PermissionError) as e:
            log.warning("sigterm %s failed: %s", l.client_pid, e)

    def _sigkill(self, l: Lease) -> None:
        if not l.client_pid:
            return
        try:
            os.kill(l.client_pid, signal.SIGKILL)
            self.store.log_event("preempt_sigkill", lease_id=l.id, pid=l.client_pid, app=l.app)
        except (ProcessLookupError, PermissionError) as e:
            log.warning("sigkill %s failed: %s", l.client_pid, e)

    def _revalidate_pids(self) -> None:
        """After daemon restart, check all active leases — release any whose
        PIDs are gone."""
        with self.lock:
            dead: list[str] = []
            for lid, l in self.broker.active_leases.items():
                if l.client_pid and not core._pid_alive_or_none(l.client_pid):
                    dead.append(lid)
            for lid in dead:
                self.broker.release(lid)
                self.store.log_event("startup_release_dead_pid", lease_id=lid)
            if dead:
                self._save_state_locked()

    # ------- preempt handler firing (called by API layer after submit) -------

    def fire_preempts(self, preempts: list[tuple[str, PreemptHandler]]) -> None:
        """Run preempt handlers off the lock (HTTP calls can block).

        We've already marked the leases as PREEMPTING in core. SIGTERM happens
        via the _preempt_loop after grace. HTTP / app-soft-preempt handlers
        fire immediately so the app can release VRAM voluntarily before any
        signal escalation."""
        for lease_id, ph in preempts:
            self.store.log_event("preempt_started", lease_id=lease_id, handler=ph.type)
            if ph.type == "http" and ph.url:
                threading.Thread(
                    target=self._fire_http_handler, args=(lease_id, ph),
                    daemon=True, name="preempt-http",
                ).start()
            elif ph.type == "comfyui_free":
                threading.Thread(
                    target=self._fire_comfyui_free, args=(lease_id, ph),
                    daemon=True, name="preempt-comfyui",
                ).start()
            elif ph.type == "forge_unload":
                threading.Thread(
                    target=self._fire_forge_unload, args=(lease_id, ph),
                    daemon=True, name="preempt-forge",
                ).start()
            # sigterm handlers are fired by the escalation loop after grace.

    def _fire_http_handler(self, lease_id: str, ph: PreemptHandler) -> None:
        try:
            body = json.dumps({"lease_id": lease_id, "deadline_s": ph.grace_s}).encode()
            req = urllib.request.Request(
                ph.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                _ = r.read()
            self.store.log_event("preempt_http_ok", lease_id=lease_id, url=ph.url)
        except Exception as e:
            self.store.log_event("preempt_http_failed", lease_id=lease_id, url=ph.url, error=str(e))

    def _fire_comfyui_free(self, lease_id: str, ph: PreemptHandler) -> None:
        """POST /free to ComfyUI to unload all models without killing the
        process. Uses ph.url as the base (default http://127.0.0.1:8188)."""
        base = ph.url or "http://127.0.0.1:8188"
        url = base.rstrip("/") + "/free"
        try:
            body = json.dumps({"unload_models": True, "free_memory": True}).encode()
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                _ = r.read()
            self.store.log_event("preempt_comfyui_free_ok", lease_id=lease_id, url=url)
        except Exception as e:
            self.store.log_event("preempt_comfyui_free_failed",
                                 lease_id=lease_id, url=url, error=str(e))

    def _fire_forge_unload(self, lease_id: str, ph: PreemptHandler) -> None:
        """POST /sdapi/v1/unload-checkpoint to Forge — releases the model
        without killing the process. ph.url is the base (default
        http://127.0.0.1:7860)."""
        base = ph.url or "http://127.0.0.1:7860"
        url = base.rstrip("/") + "/sdapi/v1/unload-checkpoint"
        try:
            req = urllib.request.Request(url, data=b"",
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                _ = r.read()
            self.store.log_event("preempt_forge_unload_ok", lease_id=lease_id, url=url)
        except Exception as e:
            self.store.log_event("preempt_forge_unload_failed",
                                 lease_id=lease_id, url=url, error=str(e))


# ============================================================
# HTTP server — JSON API
# ============================================================


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class APIHandler(http.server.BaseHTTPRequestHandler):
    runtime: Runtime = None  # type: ignore[assignment] — set by serve()

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default access logs; we log selectively in handlers.
        return

    def _send(self, status: int, body: dict | list) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            d = json.loads(raw.decode("utf-8"))
            if not isinstance(d, dict):
                return {}
            return d
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        qs = urllib.parse.parse_qs(query)
        if path == "/healthz":
            self._send(200, {"ok": True, "started_at": self.runtime.started_at, "version": VERSION})
        elif path == "/status":
            self._send(200, self._build_status())
        elif path == "/leases":
            self._send(200, self._build_leases(qs.get("app", [None])[0]))
        elif path.startswith("/lease/"):
            lid = path[len("/lease/"):]
            self._send(*self._get_lease(lid))
        elif path == "/queue":
            with self.runtime.lock:
                self._send(200, {"queue": [r.to_dict() for r in self.runtime.broker.queue]})
        elif path == "/apps":
            with self.runtime.lock:
                self._send(200, {"apps": [a.to_dict() for a in self.runtime.broker.apps.values()]})
        elif path.startswith("/app/"):
            name = path[len("/app/"):]
            with self.runtime.lock:
                app = self.runtime.broker.apps.get(name)
            if app is None:
                self._send(404, {"error": "no_such_app", "name": name})
            else:
                self._send(200, app.to_dict())
        elif path == "/history":
            limit = int(qs.get("limit", ["100"])[0])
            self._send(200, {"history": self.runtime.store.recent_events(limit)})
        elif path == "/mode":
            with self.runtime.lock:
                self._send(200, {"mode": self.runtime.broker.mode.value})
        else:
            self._send(404, {"error": "not_found", "path": path})

    def do_POST(self) -> None:
        body = self._read_body()
        if self.path == "/lease":
            self._post_lease(body)
        elif self.path == "/mode":
            self._post_mode(body)
        elif self.path == "/priority":
            self._post_priority(body)
        elif self.path == "/app":
            self._post_app(body)
        elif self.path.startswith("/lease/") and self.path.endswith("/extend"):
            lid = self.path[len("/lease/"):-len("/extend")]
            self._post_extend(lid, body)
        elif self.path.startswith("/lease/") and self.path.endswith("/preempt"):
            lid = self.path[len("/lease/"):-len("/preempt")]
            self._post_manual_preempt(lid, body)
        else:
            self._send(404, {"error": "not_found", "path": self.path})

    def do_DELETE(self) -> None:
        if self.path.startswith("/lease/"):
            lid = self.path[len("/lease/"):]
            with self.runtime.lock:
                ok = self.runtime.broker.release(lid)
                if ok:
                    self.runtime.store.log_event("release", lease_id=lid)
                    self.runtime._save_state_locked()
            self._send(200 if ok else 404, {"released": ok})
        elif self.path.startswith("/app/"):
            name = self.path[len("/app/"):]
            with self.runtime.lock:
                ok = self.runtime.app_registry.delete(name)
                self.runtime.broker.apps.pop(name, None)
            self._send(200 if ok else 404, {"deleted": ok})
        else:
            self._send(404, {"error": "not_found", "path": self.path})

    # ---- handlers ----

    def _build_status(self) -> dict:
        snap = gpu_query.gpu_snapshot()
        with self.runtime.lock:
            return {
                "version": VERSION,
                "started_at": self.runtime.started_at,
                "mode": self.runtime.broker.mode.value,
                "total_vram_mib": self.runtime.broker.total_vram_mib,
                "safety_margin_mib": self.runtime.broker.safety_margin_mib,
                "committed_mib": self.runtime.broker.committed_mib(),
                "free_budget_mib": self.runtime.broker.free_budget_mib(),
                "active_lease_count": len(self.runtime.broker.active_leases),
                "queue_depth": len(self.runtime.broker.queue),
                "gpu": {
                    "available": snap.available,
                    "total_mib": snap.total_mib,
                    "used_mib": snap.used_mib,
                    "free_mib": snap.free_mib,
                    "utilization_pct": snap.utilization_pct,
                },
                "unmanaged_processes": [
                    {"pid": u.pid, "cmdline": u.cmdline[:200], "vram_mib": u.vram_mib}
                    for u in self.runtime.broker.unmanaged.values()
                ],
                "boosts": {
                    a: {"priority": p, "expires_in_s": max(0, e - time.monotonic())}
                    for a, (p, e) in self.runtime.broker.boosts.items()
                },
            }

    def _build_leases(self, app_filter: str | None) -> dict:
        with self.runtime.lock:
            active = [
                l.to_dict() for l in self.runtime.broker.active_leases.values()
                if app_filter is None or l.app == app_filter
            ]
            queued = [
                r.to_dict() for r in self.runtime.broker.queue
                if app_filter is None or r.app == app_filter
            ]
        return {"active": active, "queued": queued}

    def _get_lease(self, lid: str) -> tuple[int, dict]:
        with self.runtime.lock:
            l = self.runtime.broker.active_leases.get(lid)
            if l:
                return 200, l.to_dict()
            for r in self.runtime.broker.queue:
                if r.id == lid:
                    return 200, {"queued": True, **r.to_dict()}
        return 404, {"error": "no_such_lease", "id": lid}

    def _post_lease(self, body: dict) -> None:
        # Required.
        app = body.get("app")
        if not app:
            self._send(400, {"error": "missing_app"})
            return
        # Budget: explicit OR from app registry default.
        budget = body.get("vram_budget_mib")
        with self.runtime.lock:
            app_cfg = self.runtime.broker.apps.get(app)
        if budget is None and app_cfg:
            budget = app_cfg.default_budget_mib
        if budget is None:
            self._send(400, {"error": "missing_budget", "message": "no vram_budget_mib and no registered app default"})
            return
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            self._send(400, {"error": "bad_budget"})
            return
        # PID — explicit, or peer for unix socket (TBD; for HTTP we trust client).
        client_pid = body.get("client_pid")
        if client_pid is not None:
            try:
                client_pid = int(client_pid)
            except (TypeError, ValueError):
                self._send(400, {"error": "bad_client_pid"})
                return
        priority = body.get("priority")
        if priority is not None:
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                self._send(400, {"error": "bad_priority"})
                return
        ttl_s = body.get("ttl_s")
        reason = body.get("reason", "")
        tenant = body.get("tenant", "")
        ph_dict = body.get("preempt_handler")
        ph = PreemptHandler.from_dict(ph_dict) if ph_dict else None
        blocking = bool(body.get("blocking", True))
        wait_timeout_s = int(body.get("wait_timeout_s", 300))

        # First attempt
        with self.runtime.lock:
            res = self.runtime.broker.submit(
                app=app, vram_budget_mib=budget, client_pid=client_pid,
                priority=priority, tenant=tenant, ttl_s=ttl_s, reason=reason,
                preempt_handler=ph,
            )
            self.runtime.store.log_event(
                "lease_request", app=app, budget_mib=budget,
                pid=client_pid, priority=priority,
                granted=res.is_granted(), queued=res.is_queued(),
                error=res.error,
            )
            self.runtime._save_state_locked()

        # Fire preempt handlers OFF the lock — they might block.
        if res.preempts_to_fire:
            self.runtime.fire_preempts(res.preempts_to_fire)

        if res.is_granted():
            self._send(200, res.lease.to_dict())
            return
        if res.is_error():
            status = 409 if res.error == "cannot_fit" else 400
            self._send(status, {"error": res.error, "message": res.message})
            return

        # Queued.
        if not blocking:
            self._send(202, {
                "queued": True,
                "request_id": res.queued_request.id,
                "queue_position": self._queue_position(res.queued_request.id),
                "preempts_fired": [{"lease_id": lid, "handler": ph.type} for lid, ph in res.preempts_to_fire],
            })
            return

        # Long-poll: wait for grant or timeout.
        req_id = res.queued_request.id
        deadline = time.monotonic() + wait_timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            with self.runtime.lock:
                # Look for a lease that came from this request.
                # We match by app + budget + reason + monotonic submission (the
                # grant timestamp ≥ request submission). The simplest robust
                # match is checking if the request_id is no longer in the
                # queue AND there's a lease for this app + budget + pid that
                # wasn't there before.
                still_queued = any(r.id == req_id for r in self.runtime.broker.queue)
                if not still_queued:
                    # Either granted or timed out. Find the matching lease.
                    matched = None
                    for l in self.runtime.broker.active_leases.values():
                        if (l.app == app and l.vram_budget_mib == budget
                                and l.client_pid == (client_pid or 0)
                                and l.reason == reason):
                            matched = l
                            break
                    if matched:
                        self._send(200, matched.to_dict())
                        return
                    # Timed out in the tick loop.
                    self._send(408, {"error": "queue_timeout", "request_id": req_id})
                    return
        # We timed out client-side; request stays in the queue. Tell caller.
        self._send(408, {"error": "wait_timeout", "request_id": req_id,
                         "message": "request still queued; reissue with blocking=false to recover request_id"})

    def _queue_position(self, req_id: str) -> int:
        with self.runtime.lock:
            for i, r in enumerate(self.runtime.broker.queue):
                if r.id == req_id:
                    return i + 1
        return 0

    def _post_mode(self, body: dict) -> None:
        m = body.get("mode")
        try:
            new_mode = Mode(m)
        except ValueError:
            self._send(400, {"error": "bad_mode", "valid": [m.value for m in Mode]})
            return
        with self.runtime.lock:
            effect = self.runtime.broker.set_mode(new_mode)
            self.runtime.store.log_event("mode_changed", mode=new_mode.value,
                                         evicted=[lid for lid, _ in effect.preempts])
            self.runtime._save_state_locked()
        if effect.preempts:
            self.runtime.fire_preempts(effect.preempts)
        self._send(200, {"mode": new_mode.value,
                         "side_effects": {"evicted_leases": [lid for lid, _ in effect.preempts]}})

    def _post_priority(self, body: dict) -> None:
        app = body.get("app")
        try:
            pri = int(body["priority"])
            ttl_s = int(body.get("ttl_s", 1800))
        except (KeyError, TypeError, ValueError):
            self._send(400, {"error": "bad_priority_request"})
            return
        with self.runtime.lock:
            self.runtime.broker.set_boost(app, pri, ttl_s, time.monotonic())
            self.runtime.store.log_event("priority_boost", app=app, priority=pri, ttl_s=ttl_s)
            self.runtime._save_state_locked()
        self._send(200, {"boosted": True, "app": app, "priority": pri,
                         "effective_until_s": ttl_s})

    def _post_app(self, body: dict) -> None:
        if "name" not in body:
            self._send(400, {"error": "missing_name"})
            return
        try:
            app = AppConfig.from_dict(body)
        except Exception as e:
            self._send(400, {"error": "bad_app_config", "message": str(e)})
            return
        with self.runtime.lock:
            self.runtime.broker.apps[app.name] = app
            self.runtime.app_registry.save(app.to_dict())
            self.runtime.store.log_event("app_registered", name=app.name)
        self._send(200, app.to_dict())

    def _post_extend(self, lid: str, body: dict) -> None:
        try:
            ttl_s = int(body["ttl_s"])
        except (KeyError, ValueError, TypeError):
            self._send(400, {"error": "bad_ttl"})
            return
        with self.runtime.lock:
            l = self.runtime.broker.active_leases.get(lid)
            if not l:
                self._send(404, {"error": "no_such_lease"})
                return
            l.ttl_s = ttl_s
            # Recompute expires_at as now + ttl.
            from datetime import datetime, timezone, timedelta
            l.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat()
            self.runtime._save_state_locked()
            self._send(200, l.to_dict())

    def _post_manual_preempt(self, lid: str, body: dict) -> None:
        with self.runtime.lock:
            l = self.runtime.broker.active_leases.get(lid)
            if not l:
                self._send(404, {"error": "no_such_lease"})
                return
            if l.state == core.LeaseState.ACTIVE.value:
                l.state = core.LeaseState.PREEMPTING.value
                self.runtime.store.log_event("manual_preempt", lease_id=lid,
                                             reason=body.get("reason", ""))
                self.runtime._save_state_locked()
                self.runtime.fire_preempts([(lid, l.preempt_handler)])
            self._send(200, l.to_dict())


# ============================================================
# Entry
# ============================================================


def serve(runtime: Runtime, host: str, port: int) -> ThreadingHTTPServer:
    APIHandler.runtime = runtime
    httpd = ThreadingHTTPServer((host, port), APIHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http-server").start()
    log.info("listening on http://%s:%d", host, port)
    return httpd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="airlockd")
    ap.add_argument("--host", default=DEFAULT_HTTP_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    ap.add_argument("--state-dir", default=os.environ.get(
        "AIRLOCK_STATE_DIR",
        str(Path.home() / ".local" / "state" / "airlock"),
    ))
    ap.add_argument("--config-dir", default=os.environ.get(
        "AIRLOCK_CONFIG_DIR",
        str(Path.home() / ".config" / "airlock"),
    ))
    ap.add_argument("--total-vram-mib", type=int, default=None,
                    help="override autodetected total VRAM (for testing)")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--mode", default="priority", choices=[m.value for m in Mode])
    ap.add_argument("--foreground", action="store_true",
                    help="run in foreground (default; daemonization is the operator's job)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    state_dir = Path(args.state_dir)
    config_dir = Path(args.config_dir)
    store = Store(state_dir)
    app_registry = AppRegistry(config_dir)
    broker = Broker(mode=Mode(args.mode))
    runtime = Runtime(broker, store, app_registry,
                      total_vram_override=args.total_vram_mib)
    runtime.start()
    httpd = serve(runtime, args.host, args.port)

    def shutdown(*_a: Any) -> None:
        log.info("shutting down")
        runtime.stop()
        httpd.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Block forever.
    try:
        while not runtime._stop_flag.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
