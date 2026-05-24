"""Integration test: real daemon subprocess, real HTTP API."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_listening(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} never listened")


def _http(method: str, url: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return e.code, (json.loads(raw) if raw else {})


class DaemonHarness:
    """Spawns the daemon under tmpdir, kills it on cleanup."""
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="airlock-test-"))
        self.port = _free_port()
        self.proc = None
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self, extra_args: list[str] | None = None) -> None:
        env = dict(os.environ)
        # Stub nvidia-smi to a no-op so the poller doesn't try real GPU calls.
        stub = self.tmp / "nvidia-smi-stub"
        stub.write_text("#!/bin/sh\necho ''\nexit 0\n")
        stub.chmod(0o755)
        env["AIRLOCK_NVIDIA_SMI_PATH"] = str(stub)
        cmd = [
            sys.executable, str(SRC / "airlockd.py"),
            "--port", str(self.port),
            "--state-dir", str(self.tmp / "state"),
            "--config-dir", str(self.tmp / "config"),
            "--total-vram-mib", "24000",
            "--log-level", "WARNING",
        ] + (extra_args or [])
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_listening(self.port, timeout=8.0)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc:
            for pipe in (self.proc.stdout, self.proc.stderr, self.proc.stdin):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
        if self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)


class TestDaemon(unittest.TestCase):
    def setUp(self):
        self.h = DaemonHarness()

    def tearDown(self):
        self.h.stop()

    def test_healthz(self):
        self.h.start()
        code, j = _http("GET", f"{self.h.url}/healthz")
        self.assertEqual(code, 200)
        self.assertTrue(j["ok"])

    def test_lease_grant_and_release(self):
        self.h.start()
        code, lease = _http("POST", f"{self.h.url}/lease", {
            "app": "test", "vram_budget_mib": 4000, "client_pid": os.getpid(),
            "blocking": False,
        })
        self.assertEqual(code, 200, lease)
        lid = lease["id"]
        code, status = _http("GET", f"{self.h.url}/status")
        self.assertEqual(status["committed_mib"], 4000)
        code, j = _http("DELETE", f"{self.h.url}/lease/{lid}")
        self.assertEqual(code, 200)
        self.assertTrue(j["released"])
        code, status = _http("GET", f"{self.h.url}/status")
        self.assertEqual(status["committed_mib"], 0)

    def test_lease_queued_when_no_room(self):
        self.h.start()
        _http("POST", f"{self.h.url}/lease", {
            "app": "a", "vram_budget_mib": 20000, "client_pid": os.getpid(), "blocking": False,
        })
        code, j = _http("POST", f"{self.h.url}/lease", {
            "app": "b", "vram_budget_mib": 10000, "client_pid": os.getpid(), "blocking": False,
        })
        self.assertEqual(code, 202)
        self.assertTrue(j["queued"])
        self.assertEqual(j["queue_position"], 1)

    def test_mode_switch(self):
        self.h.start()
        code, j = _http("POST", f"{self.h.url}/mode", {"mode": "equal"})
        self.assertEqual(code, 200)
        self.assertEqual(j["mode"], "equal")
        code, j = _http("GET", f"{self.h.url}/mode")
        self.assertEqual(j["mode"], "equal")

    def test_app_register_and_list(self):
        self.h.start()
        code, j = _http("POST", f"{self.h.url}/app", {
            "name": "comfyui",
            "default_budget_mib": 14000,
            "default_priority": 60,
            "cmdline_match": "python.*ComfyUI",
        })
        self.assertEqual(code, 200)
        code, j = _http("GET", f"{self.h.url}/apps")
        names = [a["name"] for a in j["apps"]]
        self.assertIn("comfyui", names)
        # Lease without explicit budget should pull from app default
        code, l = _http("POST", f"{self.h.url}/lease", {
            "app": "comfyui", "client_pid": os.getpid(), "blocking": False,
        })
        self.assertEqual(code, 200)
        self.assertEqual(l["vram_budget_mib"], 14000)

    def test_priority_boost(self):
        self.h.start()
        code, j = _http("POST", f"{self.h.url}/priority", {
            "app": "x", "priority": 95, "ttl_s": 120,
        })
        self.assertEqual(code, 200)
        code, status = _http("GET", f"{self.h.url}/status")
        self.assertIn("x", status["boosts"])
        self.assertEqual(status["boosts"]["x"]["priority"], 95)

    def test_state_persists_across_restart(self):
        self.h.start()
        _http("POST", f"{self.h.url}/app", {
            "name": "stickyapp", "default_budget_mib": 3000, "default_priority": 40,
        })
        _http("POST", f"{self.h.url}/lease", {
            "app": "stickyapp", "vram_budget_mib": 3000, "client_pid": os.getpid(),
            "blocking": False,
        })
        # Stop daemon, restart, verify state restored.
        self.h.proc.send_signal(signal.SIGTERM)
        self.h.proc.wait(timeout=5)
        for pipe in (self.h.proc.stdout, self.h.proc.stderr):
            if pipe is not None:
                pipe.close()
        time.sleep(0.5)
        self.h.proc = subprocess.Popen(
            [sys.executable, str(SRC / "airlockd.py"),
             "--port", str(self.h.port),
             "--state-dir", str(self.h.tmp / "state"),
             "--config-dir", str(self.h.tmp / "config"),
             "--total-vram-mib", "24000",
             "--log-level", "WARNING"],
            env={**os.environ, "AIRLOCK_NVIDIA_SMI_PATH": str(self.h.tmp / "nvidia-smi-stub")},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _wait_listening(self.h.port, timeout=8.0)
        code, status = _http("GET", f"{self.h.url}/status")
        # PID is alive (us), so lease should have been preserved
        self.assertEqual(status["active_lease_count"], 1)
        self.assertEqual(status["committed_mib"], 3000)
        code, apps = _http("GET", f"{self.h.url}/apps")
        names = [a["name"] for a in apps["apps"]]
        self.assertIn("stickyapp", names)

    def test_dead_pid_auto_released(self):
        self.h.start()
        # Spawn a tiny short-lived subprocess and acquire a lease bound to it
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        code, lease = _http("POST", f"{self.h.url}/lease", {
            "app": "ghost", "vram_budget_mib": 1000, "client_pid": child.pid,
            "blocking": False,
        })
        self.assertEqual(code, 200)
        child.kill()
        child.wait()
        # Watchdog runs every 2s; give it 4.
        time.sleep(4)
        code, status = _http("GET", f"{self.h.url}/status")
        self.assertEqual(status["active_lease_count"], 0)

    def test_cannot_fit_error(self):
        self.h.start()
        code, j = _http("POST", f"{self.h.url}/lease", {
            "app": "huge", "vram_budget_mib": 30000, "client_pid": os.getpid(),
            "blocking": False,
        })
        self.assertEqual(code, 409)
        self.assertEqual(j["error"], "cannot_fit")


if __name__ == "__main__":
    unittest.main()
