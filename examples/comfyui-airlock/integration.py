"""Airlock <-> ComfyUI integration.

Two side-effects on import:
1. Register `comfyui` as an app with airlockd, with preempt_handler =
   comfyui_free pointing back at this ComfyUI's server URL.
2. Monkey-patch comfy.execution.PromptExecutor.execute to wrap each prompt
   in a try/finally that acquires a lease before, releases on completion.

Plus a custom HTTP route on /airlock/release that lets airlock instruct this
ComfyUI to drop models on demand (used as the preempt handler).

Fail-open everywhere: if airlockd is unreachable, ComfyUI runs unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("airlock.comfyui")


AIRLOCK_URL = os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8447")
APP_NAME = os.environ.get("AIRLOCK_APP_NAME", "comfyui")
DEFAULT_BUDGET_MIB = int(os.environ.get("AIRLOCK_DEFAULT_BUDGET_MIB", "14336"))
DEFAULT_PRIORITY = int(os.environ.get("AIRLOCK_PRIORITY", "60"))
DISABLED = os.environ.get("AIRLOCK_DISABLED", "").strip() not in ("", "0", "false", "no")

# Try to detect the local ComfyUI listen URL so airlock can POST /free back.
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
SELF_URL = os.environ.get("AIRLOCK_COMFY_SELF_URL", f"http://127.0.0.1:{COMFY_PORT}")


# ---------- Tiny airlock HTTP client (no deps, fail-open) ----------


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
        req = urllib.request.Request(
            AIRLOCK_URL.rstrip("/") + path, method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except (urllib.error.URLError, OSError) as e:
        log.debug("airlock DELETE %s failed: %s", path, e)
        return False


def _register_app() -> None:
    """Tell airlock about us. Idempotent (POST /app is upsert)."""
    body = {
        "name": APP_NAME,
        "default_budget_mib": DEFAULT_BUDGET_MIB,
        "default_priority": DEFAULT_PRIORITY,
        "preempt_handler": {"type": "comfyui_free", "url": SELF_URL, "grace_s": 30},
        "cmdline_match": r"main\.py.*--port\s+" + str(COMFY_PORT),
        "watch_implicit": True,
    }
    r = _airlock_post("/app", body)
    if r is not None:
        log.info("airlock: registered as %s (budget %dMiB pri %d)",
                 APP_NAME, DEFAULT_BUDGET_MIB, DEFAULT_PRIORITY)


# ---------- Workflow VRAM estimation ----------


def _estimate_budget_from_workflow(prompt: dict) -> int:
    """Best-effort estimate of VRAM needed for this workflow. ComfyUI's
    prompt is a dict of node_id -> {class_type, inputs}. We sum heuristics
    based on what model-loading nodes are present.

    Default fallback = DEFAULT_BUDGET_MIB if nothing matches."""
    if not isinstance(prompt, dict):
        return DEFAULT_BUDGET_MIB
    total = 0
    for nid, node in prompt.items():
        if not isinstance(node, dict):
            continue
        ct = (node.get("class_type") or "").lower()
        # SDXL checkpoint loaders
        if "checkpointloader" in ct or "unetloader" in ct:
            total += 6000
        elif "fluxloader" in ct or "flux" in ct:
            total += 12000  # FLUX models are large
        elif "vaeloader" in ct:
            total += 500
        elif "cliploader" in ct or "dualcliploader" in ct:
            total += 1500
        elif "controlnet" in ct:
            total += 1500
        elif "lora" in ct:
            total += 200
        elif "upscale" in ct:
            total += 500
    # Activations + scratch — add 30%
    total = int(total * 1.3)
    return max(total, DEFAULT_BUDGET_MIB) if total > 0 else DEFAULT_BUDGET_MIB


# ---------- The monkey-patch ----------


def _install_executor_hook() -> None:
    """Wrap execution.PromptExecutor.execute to acquire/release a lease."""
    try:
        import execution
    except ImportError:
        log.warning("airlock: can't import comfyui's execution module — hook not installed")
        return

    PromptExecutor = getattr(execution, "PromptExecutor", None)
    if PromptExecutor is None or not hasattr(PromptExecutor, "execute"):
        log.warning("airlock: PromptExecutor.execute not found — hook not installed")
        return

    original_execute = PromptExecutor.execute

    def execute_with_lease(self, prompt: dict, prompt_id: str,
                           extra_data: dict | None = None,
                           execute_outputs: list | None = None) -> Any:
        if DISABLED:
            return original_execute(self, prompt, prompt_id, extra_data, execute_outputs)
        budget = _estimate_budget_from_workflow(prompt)
        body = {
            "app": APP_NAME,
            "vram_budget_mib": budget,
            "client_pid": os.getpid(),
            "reason": f"prompt {prompt_id[:8]}",
            "blocking": True,
            "wait_timeout_s": 600,
        }
        lease = _airlock_post("/lease", body, timeout=605)
        lease_id = lease.get("id") if isinstance(lease, dict) else None
        if lease_id:
            log.info("airlock: lease %s acquired for prompt %s (budget %dMiB)",
                     lease_id, prompt_id[:8], budget)
        try:
            return original_execute(self, prompt, prompt_id, extra_data, execute_outputs)
        finally:
            if lease_id:
                _airlock_delete(f"/lease/{lease_id}")
                log.info("airlock: lease %s released", lease_id)

    PromptExecutor.execute = execute_with_lease
    log.info("airlock: hooked PromptExecutor.execute")


# ---------- Preempt handler endpoint (airlock POSTs here to free VRAM) ----------


def _install_release_route() -> None:
    """Register /airlock/release on ComfyUI's aiohttp server. airlockd's
    comfyui_free preempt handler POSTs ComfyUI's own /free, so this route
    is largely redundant but useful as an explicit fallback for callers
    that prefer to talk to /airlock/release."""
    try:
        from server import PromptServer
    except ImportError:
        log.warning("airlock: PromptServer not available — release route not installed")
        return

    routes = PromptServer.instance.routes

    @routes.post("/airlock/release")  # type: ignore[misc]
    async def airlock_release(request):  # type: ignore[no-untyped-def]
        try:
            import comfy.model_management as mm
            mm.unload_all_models()
            mm.soft_empty_cache()
            return _aio_json({"ok": True, "released": True})
        except Exception as e:
            log.exception("airlock release failed")
            return _aio_json({"ok": False, "error": str(e)}, status=500)

    log.info("airlock: registered /airlock/release route")


def _aio_json(body: dict, status: int = 200):
    from aiohttp import web
    return web.json_response(body, status=status)


# ---------- Boot ----------


def _boot_in_background() -> None:
    """Defer init slightly so ComfyUI server is fully up before we try to
    register routes / register with airlock."""
    if DISABLED:
        log.info("airlock: integration disabled via AIRLOCK_DISABLED")
        return
    time.sleep(2)
    _install_executor_hook()
    _install_release_route()
    _register_app()


threading.Thread(target=_boot_in_background, daemon=True, name="airlock-init").start()
