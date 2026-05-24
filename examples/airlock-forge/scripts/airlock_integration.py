"""Forge Script that acquires an airlock lease per generation.

Forge auto-discovers scripts under extensions/*/scripts/. Subclassing
modules.scripts.Script registers it for all txt2img / img2img tabs and API
calls. `process()` runs before generation, `postprocess()` after — exactly
the acquire/release boundaries we need.

Registers `forge` as an app with airlockd on startup via on_app_started
callback. Preempt handler `forge_unload` points at this Forge's port so
airlockd can POST /sdapi/v1/unload-checkpoint to release VRAM without kill.

Fail-open: if airlockd is unreachable, generation proceeds without a lease.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request

from modules import scripts, script_callbacks


log = logging.getLogger("airlock.forge")


AIRLOCK_URL = os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8447")
APP_NAME = os.environ.get("AIRLOCK_FORGE_APP_NAME", "forge")
DEFAULT_BUDGET_MIB = int(os.environ.get("AIRLOCK_FORGE_DEFAULT_BUDGET_MIB", "10240"))
DEFAULT_PRIORITY = int(os.environ.get("AIRLOCK_FORGE_PRIORITY", "60"))
DISABLED = os.environ.get("AIRLOCK_DISABLED", "").strip() not in ("", "0", "false", "no")

FORGE_PORT = int(os.environ.get("FORGE_PORT", "7860"))
SELF_URL = os.environ.get("AIRLOCK_FORGE_SELF_URL", f"http://127.0.0.1:{FORGE_PORT}")


def _airlock_post(path: str, body: dict, timeout: float = 5.0) -> dict | None:
    try:
        req = urllib.request.Request(
            AIRLOCK_URL.rstrip("/") + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.debug("airlock POST %s failed: %s", path, e)
        return None


def _airlock_delete(path: str, timeout: float = 5.0) -> bool:
    try:
        req = urllib.request.Request(AIRLOCK_URL.rstrip("/") + path, method="DELETE")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def _register_app() -> None:
    body = {
        "name": APP_NAME,
        "default_budget_mib": DEFAULT_BUDGET_MIB,
        "default_priority": DEFAULT_PRIORITY,
        "preempt_handler": {"type": "forge_unload", "url": SELF_URL, "grace_s": 30},
        "cmdline_match": r"launch\.py.*--listen",
        "watch_implicit": True,
    }
    if _airlock_post("/app", body) is not None:
        log.info("airlock: registered as %s (budget %dMiB pri %d)",
                 APP_NAME, DEFAULT_BUDGET_MIB, DEFAULT_PRIORITY)


def _on_app_started(*_a, **_kw) -> None:
    if DISABLED:
        return
    threading.Thread(target=_register_app, daemon=True, name="airlock-register").start()


script_callbacks.on_app_started(_on_app_started)


class AirlockLeaseScript(scripts.Script):
    """Forge Script that wraps generation in an airlock lease.

    Forge calls process(p) before generation, postprocess(p, ...) after.
    Both run for UI clicks AND /sdapi/v1/txt2img | img2img calls.
    """

    def __init__(self):
        super().__init__()
        self._lease_id: str | None = None

    def title(self) -> str:
        return "airlock lease"

    def show(self, is_img2img: bool) -> bool:
        # AlwaysVisible-equivalent: returns scripts.AlwaysVisible would surface
        # UI controls; we just want process()/postprocess() to fire always.
        return scripts.AlwaysVisible

    def ui(self, is_img2img: bool):
        # No UI — script is silent infrastructure.
        return []

    def process(self, p, *args, **kwargs) -> None:
        if DISABLED:
            return
        # p.width, p.height, p.batch_size are available; for SDXL we'd add
        # them into the budget estimate, but for v1 we just use the default
        # registered budget.
        body = {
            "app": APP_NAME,
            "client_pid": os.getpid(),
            "reason": f"{type(p).__name__}",
            "blocking": True,
            "wait_timeout_s": 600,
        }
        result = _airlock_post("/lease", body, timeout=605)
        if isinstance(result, dict):
            self._lease_id = result.get("id")
            if self._lease_id:
                log.info("airlock: lease %s acquired for %s",
                         self._lease_id, type(p).__name__)

    def postprocess(self, p, processed, *args, **kwargs) -> None:
        if self._lease_id:
            _airlock_delete(f"/lease/{self._lease_id}")
            log.info("airlock: lease %s released", self._lease_id)
            self._lease_id = None
