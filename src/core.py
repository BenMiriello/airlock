"""Core lease state machine. Pure-ish logic, no I/O, no threads.

The Broker class is the single source of truth. It exposes methods to:
  - submit a lease request (returns Lease or queued Request)
  - release a lease
  - tick (re-evaluate the queue after state changes)
  - update_actual_usage (from the nvidia-smi poller)
  - mark a process dead (from the PID watchdog)
  - set mode / set boost
  - snapshot/restore state from a dict

External actors (preempt handlers, persistence) are notified via a small
event hook the caller passes in. The Broker NEVER performs I/O itself.
That keeps it deterministic and unit-testable.

Three modes:
  - exclusive: one lease at a time
  - priority:  multiple leases coexist within budget; high-priority preempts low
  - equal:     multiple leases coexist within budget; FCFS, no preemption

All VRAM values are in MiB. Time is monotonic seconds (passed in by caller).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional


def _new_id(prefix: str) -> str:
    """Lease/request IDs. Short, sortable-ish (timestamp-prefixed), unique."""
    return f"{prefix}_{int(time.time()*1000):x}{secrets.token_hex(3)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Mode(str, Enum):
    EXCLUSIVE = "exclusive"
    PRIORITY = "priority"
    EQUAL = "equal"


class LeaseState(str, Enum):
    ACTIVE = "active"
    PREEMPTING = "preempting"
    RELEASED = "released"


@dataclass
class PreemptHandler:
    """How to ask a lease holder to yield. Either SIGTERM the PID or POST
    to an HTTP URL and wait for the holder to release on its own."""
    type: str  # "sigterm" | "http"
    url: Optional[str] = None
    grace_s: int = 30

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PreemptHandler":
        if not d:
            return cls(type="sigterm")
        return cls(type=d.get("type", "sigterm"), url=d.get("url"), grace_s=int(d.get("grace_s", 30)))

    def to_dict(self) -> dict:
        return {"type": self.type, "url": self.url, "grace_s": self.grace_s}


@dataclass
class Lease:
    id: str
    app: str
    tenant: str
    client_pid: int
    vram_budget_mib: int
    priority: int
    acquired_at: str
    ttl_s: Optional[int]
    expires_at: Optional[str]
    reason: str
    preempt_handler: PreemptHandler
    state: str = LeaseState.ACTIVE.value
    vram_actual_mib: int = 0
    vram_peak_mib: int = 0      # high-water mark of observed actual
    implicit: bool = False       # true if created from observation, not request

    def to_dict(self) -> dict:
        d = asdict(self)
        d["preempt_handler"] = self.preempt_handler.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        ph = PreemptHandler.from_dict(d.get("preempt_handler"))
        return cls(
            id=d["id"], app=d["app"], tenant=d.get("tenant", ""),
            client_pid=int(d["client_pid"]), vram_budget_mib=int(d["vram_budget_mib"]),
            priority=int(d["priority"]), acquired_at=d["acquired_at"],
            ttl_s=d.get("ttl_s"), expires_at=d.get("expires_at"),
            reason=d.get("reason", ""), preempt_handler=ph,
            state=d.get("state", LeaseState.ACTIVE.value),
            vram_actual_mib=int(d.get("vram_actual_mib", 0)),
            vram_peak_mib=int(d.get("vram_peak_mib", 0)),
            implicit=bool(d.get("implicit", False)),
        )


@dataclass
class Request:
    """A pending lease request, sitting in the queue."""
    id: str
    app: str
    tenant: str
    client_pid: Optional[int]
    vram_budget_mib: int
    priority: int
    submitted_at: str
    submitted_at_monotonic: float
    reason: str
    preempt_handler: PreemptHandler
    ttl_s: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["preempt_handler"] = self.preempt_handler.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Request":
        return cls(
            id=d["id"], app=d["app"], tenant=d.get("tenant", ""),
            client_pid=d.get("client_pid"),
            vram_budget_mib=int(d["vram_budget_mib"]),
            priority=int(d["priority"]),
            submitted_at=d["submitted_at"],
            submitted_at_monotonic=float(d.get("submitted_at_monotonic", 0.0)),
            reason=d.get("reason", ""),
            preempt_handler=PreemptHandler.from_dict(d.get("preempt_handler")),
            ttl_s=d.get("ttl_s"),
        )


@dataclass
class UnmanagedProcess:
    pid: int
    cmdline: str
    vram_mib: int
    first_seen_at: str
    last_seen_at: str


@dataclass
class AppConfig:
    name: str
    default_budget_mib: int = 1000
    default_priority: int = 50
    preempt_handler: PreemptHandler = field(default_factory=lambda: PreemptHandler("sigterm"))
    cmdline_match: Optional[str] = None
    watch_implicit: bool = True
    enforce_budget_strict: bool = False
    launch: Optional[dict] = None  # {"user": ..., "command": [...], "env": {...}}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["preempt_handler"] = self.preempt_handler.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        return cls(
            name=d["name"],
            default_budget_mib=int(d.get("default_budget_mib", 1000)),
            default_priority=int(d.get("default_priority", 50)),
            preempt_handler=PreemptHandler.from_dict(d.get("preempt_handler")),
            cmdline_match=d.get("cmdline_match"),
            watch_implicit=bool(d.get("watch_implicit", True)),
            enforce_budget_strict=bool(d.get("enforce_budget_strict", False)),
            launch=d.get("launch"),
        )


@dataclass
class GrantResult:
    """Outcome of a lease request, returned by submit() / try_grant()."""
    lease: Optional[Lease] = None
    queued_request: Optional[Request] = None
    error: Optional[str] = None
    message: Optional[str] = None
    # Preempts the runtime layer should fire to make room for this request.
    # Populated when priority-mode decides to preempt; the queued_request will
    # be granted on a subsequent tick once those leases release.
    preempts_to_fire: list[tuple[str, PreemptHandler]] = field(default_factory=list)

    def is_granted(self) -> bool:
        return self.lease is not None

    def is_queued(self) -> bool:
        return self.queued_request is not None

    def is_error(self) -> bool:
        return self.error is not None


@dataclass
class TickEffect:
    """Side-effects the broker has decided on during a tick. The runtime
    layer applies them (fires preempt handlers, persists, etc).

    Each preempt is (lease_id, preempt_handler). Each grant is a Lease."""
    preempts: list[tuple[str, PreemptHandler]] = field(default_factory=list)
    grants: list[Lease] = field(default_factory=list)
    timeouts: list[Request] = field(default_factory=list)  # requests that timed out


# ============================================================
# Broker — pure state machine
# ============================================================


class Broker:
    def __init__(
        self,
        total_vram_mib: int = 24576,
        safety_margin_mib: int = 512,
        mode: Mode = Mode.PRIORITY,
    ):
        self.total_vram_mib = total_vram_mib
        self.safety_margin_mib = safety_margin_mib
        self.mode: Mode = mode
        self.active_leases: dict[str, Lease] = {}      # by lease_id
        self.queue: list[Request] = []                  # FIFO + priority
        self.apps: dict[str, AppConfig] = {}            # by app name
        self.unmanaged: dict[int, UnmanagedProcess] = {}
        # Priority boosts: app_name -> (priority, expires_at_monotonic)
        self.boosts: dict[str, tuple[int, float]] = {}

    # --- helpers ---

    def usable_total(self) -> int:
        return self.total_vram_mib - self.safety_margin_mib

    def committed_mib(self) -> int:
        # PREEMPTING leases still occupy VRAM until they actually release —
        # exclude only RELEASED. This prevents granting a queued request to
        # space that hasn't actually been freed yet.
        return sum(l.vram_budget_mib for l in self.active_leases.values()
                   if l.state != LeaseState.RELEASED.value)

    def free_budget_mib(self) -> int:
        unmanaged_total = sum(u.vram_mib for u in self.unmanaged.values())
        return max(0, self.usable_total() - self.committed_mib() - unmanaged_total)

    def effective_priority(self, app: str, base_priority: int, now_mono: float) -> int:
        """Apply active boost overrides. Highest of (base, boost) wins."""
        boost = self.boosts.get(app)
        if boost is None:
            return base_priority
        boost_pri, expires = boost
        if now_mono >= expires:
            del self.boosts[app]
            return base_priority
        return max(base_priority, boost_pri)

    # --- mode + boost ---

    def set_mode(self, new_mode: Mode) -> TickEffect:
        """Switching to exclusive while >1 lease active preempts all but
        the highest-priority holder."""
        self.mode = new_mode
        effect = TickEffect()
        if new_mode == Mode.EXCLUSIVE and len(self.active_leases) > 1:
            # Keep the highest-priority lease, preempt the rest.
            sorted_leases = sorted(
                self.active_leases.values(),
                key=lambda l: -l.priority,
            )
            keeper = sorted_leases[0]
            for l in sorted_leases[1:]:
                if l.state == LeaseState.ACTIVE.value:
                    l.state = LeaseState.PREEMPTING.value
                    effect.preempts.append((l.id, l.preempt_handler))
        return effect

    def set_boost(self, app: str, priority: int, ttl_s: int, now_mono: float) -> None:
        self.boosts[app] = (priority, now_mono + ttl_s)

    # --- requests ---

    def submit(
        self,
        app: str,
        vram_budget_mib: int,
        client_pid: Optional[int],
        priority: Optional[int] = None,
        tenant: str = "",
        ttl_s: Optional[int] = None,
        reason: str = "",
        preempt_handler: Optional[PreemptHandler] = None,
        now_mono: Optional[float] = None,
    ) -> GrantResult:
        """Submit a new lease request. Returns either a granted Lease, a
        queued Request, or an error."""
        if now_mono is None:
            now_mono = time.monotonic()
        cfg = self.apps.get(app)
        if priority is None:
            priority = cfg.default_priority if cfg else 50
        if preempt_handler is None:
            preempt_handler = cfg.preempt_handler if cfg else PreemptHandler("sigterm")
        if vram_budget_mib <= 0:
            return GrantResult(error="bad_budget", message="vram_budget_mib must be > 0")
        if vram_budget_mib + self.safety_margin_mib > self.total_vram_mib:
            return GrantResult(
                error="cannot_fit",
                message=f"budget {vram_budget_mib} exceeds total - margin "
                        f"({self.total_vram_mib} - {self.safety_margin_mib})",
            )

        effective_pri = self.effective_priority(app, priority, now_mono)
        req = Request(
            id=_new_id("req"),
            app=app, tenant=tenant, client_pid=client_pid,
            vram_budget_mib=vram_budget_mib, priority=effective_pri,
            submitted_at=_now_iso(),
            submitted_at_monotonic=now_mono,
            reason=reason, preempt_handler=preempt_handler,
            ttl_s=ttl_s,
        )

        # Mode-specific gating
        if self.mode == Mode.EXCLUSIVE and self.active_leases:
            # Cannot grant — queue
            self.queue.append(req)
            self._sort_queue()
            return GrantResult(queued_request=req)

        # Try to fit immediately
        if vram_budget_mib <= self.free_budget_mib():
            return GrantResult(lease=self._grant_request(req, now_mono))

        # In priority mode, try to preempt
        if self.mode == Mode.PRIORITY:
            preemptable = self._find_preemptable(req)
            if preemptable:
                # We CAN make room — queue + tell caller which leases to preempt.
                self.queue.append(req)
                self._sort_queue()
                preempts: list[tuple[str, PreemptHandler]] = []
                for l in preemptable:
                    if l.state == LeaseState.ACTIVE.value:
                        l.state = LeaseState.PREEMPTING.value
                        preempts.append((l.id, l.preempt_handler))
                return GrantResult(queued_request=req, preempts_to_fire=preempts)

        # Equal mode or no preemptable: just queue.
        self.queue.append(req)
        self._sort_queue()
        return GrantResult(queued_request=req)

    def submit_implicit(
        self,
        app: str,
        client_pid: int,
        observed_vram_mib: int,
        cmdline: str = "",
        now_mono: Optional[float] = None,
    ) -> Optional[Lease]:
        """Create an implicit lease for a newly-observed PID matching a
        registered app. Returns the new lease, or None if app unknown / not
        watched."""
        if now_mono is None:
            now_mono = time.monotonic()
        cfg = self.apps.get(app)
        if cfg is None or not cfg.watch_implicit:
            return None
        # Implicit leases get the bigger of (default_budget, observed * 1.2)
        # rounded up so the budget is honest.
        budget = max(cfg.default_budget_mib, int(observed_vram_mib * 1.2))
        lease = Lease(
            id=_new_id("lease"),
            app=app, tenant="", client_pid=client_pid,
            vram_budget_mib=budget,
            priority=self.effective_priority(app, cfg.default_priority, now_mono),
            acquired_at=_now_iso(),
            ttl_s=None, expires_at=None,
            reason=f"implicit (cmdline match)",
            preempt_handler=cfg.preempt_handler,
            vram_actual_mib=observed_vram_mib,
            vram_peak_mib=observed_vram_mib,
            implicit=True,
        )
        self.active_leases[lease.id] = lease
        return lease

    def _grant_request(self, req: Request, now_mono: float) -> Lease:
        """Materialize a queued/pending Request into an active Lease."""
        expires_at = None
        if req.ttl_s:
            expires_at = datetime.now(timezone.utc).isoformat()  # caller sets real expiry from ttl
        lease = Lease(
            id=_new_id("lease"),
            app=req.app, tenant=req.tenant,
            client_pid=req.client_pid or 0,
            vram_budget_mib=req.vram_budget_mib,
            priority=req.priority,
            acquired_at=_now_iso(),
            ttl_s=req.ttl_s,
            expires_at=expires_at,
            reason=req.reason,
            preempt_handler=req.preempt_handler,
        )
        self.active_leases[lease.id] = lease
        return lease

    def release(self, lease_id: str) -> bool:
        """Release a lease. Returns True if it was active, False if unknown."""
        if lease_id in self.active_leases:
            del self.active_leases[lease_id]
            return True
        return False

    def remove_unmanaged(self, pid: int) -> None:
        self.unmanaged.pop(pid, None)

    # --- queue + preempt logic ---

    def _sort_queue(self) -> None:
        """Sort queue: higher priority first; within priority, FCFS by
        submission time."""
        self.queue.sort(key=lambda r: (-r.priority, r.submitted_at_monotonic))

    def _find_preemptable(self, req: Request) -> Optional[list[Lease]]:
        """Find the smallest set of active leases with priority strictly LESS
        than the request's, whose combined release would make room.

        Returns None if no combination works (caller queues the request
        instead)."""
        # Candidates: any active lease (not already preempting) with priority < req's
        candidates = [
            l for l in self.active_leases.values()
            if l.state == LeaseState.ACTIVE.value and l.priority < req.priority
        ]
        if not candidates:
            return None
        # Sort by priority ascending (kill weakest first), then by largest budget
        # (frees most VRAM per kill)
        candidates.sort(key=lambda l: (l.priority, -l.vram_budget_mib))

        needed = req.vram_budget_mib - self.free_budget_mib()
        if needed <= 0:
            return []
        picked: list[Lease] = []
        freed = 0
        for l in candidates:
            picked.append(l)
            freed += l.vram_budget_mib
            if freed >= needed:
                return picked
        return None  # not enough preemptable budget exists

    def tick(self, now_mono: Optional[float] = None, wait_timeout_s: int = 300) -> TickEffect:
        """Re-evaluate the queue: grant anything that now fits; expire
        timed-out requests. Called after any state change AND periodically
        by the runtime.

        Note: preempts triggered during submit() are already in the request's
        ._pending_preempts and the caller fires them. This tick handles only
        "queue something just got room" cases."""
        if now_mono is None:
            now_mono = time.monotonic()
        effect = TickEffect()

        # 1. Expire stale queue requests (clients gave up / wrapper died)
        keep_queue: list[Request] = []
        for r in self.queue:
            age = now_mono - r.submitted_at_monotonic
            if r.client_pid and not _pid_alive_or_none(r.client_pid):
                effect.timeouts.append(r)
                continue
            if age > wait_timeout_s:
                effect.timeouts.append(r)
                continue
            keep_queue.append(r)
        self.queue = keep_queue

        # 2. TTL'd lease expiry
        now_iso = datetime.now(timezone.utc).isoformat()
        for lease_id, l in list(self.active_leases.items()):
            if l.expires_at and l.expires_at < now_iso:
                del self.active_leases[lease_id]

        # 3. Grant from queue: pop highest priority that fits NOW.
        # Don't process queue if mode is exclusive and we still have a lease.
        progressed = True
        while progressed:
            progressed = False
            if self.mode == Mode.EXCLUSIVE and self.active_leases:
                break
            self._sort_queue()
            for i, r in enumerate(self.queue):
                if r.vram_budget_mib <= self.free_budget_mib():
                    self.queue.pop(i)
                    lease = self._grant_request(r, now_mono)
                    effect.grants.append(lease)
                    progressed = True
                    break

        # 4. Clean expired boosts
        for app in list(self.boosts):
            _, expires = self.boosts[app]
            if now_mono >= expires:
                del self.boosts[app]

        return effect

    # --- observation feedback (from poller + watchdog) ---

    def update_actual_usage(self, pid: int, vram_mib: int) -> None:
        """Update vram_actual_mib (and high-water peak) for whichever lease
        holds this PID."""
        for l in self.active_leases.values():
            if l.client_pid == pid:
                l.vram_actual_mib = vram_mib
                if vram_mib > l.vram_peak_mib:
                    l.vram_peak_mib = vram_mib
                return

    def decay_implicit_budgets(
        self,
        decay_factor: float = 1.2,
        floor_mib: int = 256,
        relative_slack: float = 0.5,
    ) -> list[tuple[str, int, int]]:
        """For implicit leases where the declared budget is much larger than the
        observed peak, shrink the budget toward `peak * decay_factor`. Keeps
        accounting honest as apps drop from a one-time spike to steady-state.

        Only shrinks when budget exceeds peak by more than relative_slack
        (default 50%) to avoid thrashing.

        Returns list of (lease_id, old_budget, new_budget) for events logged
        by the caller."""
        changes: list[tuple[str, int, int]] = []
        for l in self.active_leases.values():
            if not l.implicit:
                continue
            peak = max(l.vram_peak_mib, l.vram_actual_mib, floor_mib)
            target = max(int(peak * decay_factor), floor_mib)
            if target < l.vram_budget_mib * (1 - relative_slack):
                old = l.vram_budget_mib
                l.vram_budget_mib = target
                changes.append((l.id, old, target))
        return changes

    def see_unmanaged(self, pid: int, cmdline: str, vram_mib: int) -> None:
        u = self.unmanaged.get(pid)
        now = _now_iso()
        if u is None:
            self.unmanaged[pid] = UnmanagedProcess(
                pid=pid, cmdline=cmdline, vram_mib=vram_mib,
                first_seen_at=now, last_seen_at=now,
            )
        else:
            u.vram_mib = vram_mib
            u.last_seen_at = now

    def forget_pid(self, pid: int) -> list[str]:
        """PID gone. Release all leases held by it. Returns the released lease IDs."""
        released = []
        for lease_id, l in list(self.active_leases.items()):
            if l.client_pid == pid:
                del self.active_leases[lease_id]
                released.append(lease_id)
        self.remove_unmanaged(pid)
        return released

    # --- snapshot / restore ---

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "mode": self.mode.value,
            "total_vram_mib": self.total_vram_mib,
            "safety_margin_mib": self.safety_margin_mib,
            "active_leases": [l.to_dict() for l in self.active_leases.values()],
            "queue": [r.to_dict() for r in self.queue],
            "boosts": {a: {"priority": p, "expires_in_s": max(0, e - time.monotonic())}
                       for a, (p, e) in self.boosts.items()},
            "apps": {n: c.to_dict() for n, c in self.apps.items()},
        }

    def restore(self, snap: dict) -> None:
        self.mode = Mode(snap.get("mode", "priority"))
        self.total_vram_mib = int(snap.get("total_vram_mib", self.total_vram_mib))
        self.safety_margin_mib = int(snap.get("safety_margin_mib", self.safety_margin_mib))
        self.active_leases = {
            d["id"]: Lease.from_dict(d) for d in snap.get("active_leases", [])
        }
        self.queue = [Request.from_dict(d) for d in snap.get("queue", [])]
        self.apps = {
            n: AppConfig.from_dict(d) for n, d in snap.get("apps", {}).items()
        }
        # Boosts: convert relative expires_in_s back to absolute monotonic
        now_mono = time.monotonic()
        self.boosts = {
            a: (int(v["priority"]), now_mono + float(v.get("expires_in_s", 0)))
            for a, v in snap.get("boosts", {}).items()
        }


def _pid_alive_or_none(pid: Optional[int]) -> bool:
    """Returns True if pid is None (we don't track this client) or PID exists.
    Returns False only when we know it's dead."""
    if pid is None or pid == 0:
        return True
    try:
        import os
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
