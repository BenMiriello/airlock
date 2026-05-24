# airlock — GPU traffic manager

A small, durable, host-level daemon that owns the single GPU. Every consumer
on the box — custom apps we wrote, foreign apps like ComfyUI, ad-hoc shell
scripts — coordinates through it. The goal is **no OOMs across apps**, and a
clean, queryable picture of who's using the card at any moment.

This document is the design + spec + integration guide. After it is
implemented, this file remains as the README + reference.

---

## 1. What it is and isn't

**It is:**
- A reservation broker. One daemon. Owns the source of truth for "who can use
  the GPU right now, and how much VRAM are they allowed."
- A queue. Pending requests wait until VRAM is available; servable requests
  proceed.
- An observer. Polls `nvidia-smi` constantly so it knows what's actually
  happening on the GPU, even processes that didn't ask permission.
- A small enforcement layer. Can preempt cooperative apps (via their
  registered preempt handlers) and SIGTERM as escalation.

**It is not:**
- A GPU virtualizer. The 3090 still serves one process at a time at the
  driver level; the broker just decides which processes get the chance.
- A model registry. It knows VRAM budgets, not what's loaded in them.
- A scheduler for compute. It only mediates memory residency.
- A multi-user permission system. Single operator, no auth on the local API.

---

## 2. Naming, paths, layout

| What | Where |
|---|---|
| Daemon binary | `/usr/local/bin/airlockd` (eventually); `~/airlock/src/airlockd.py` during dev |
| CLI | `/usr/local/bin/airlock`; `~/airlock/src/airlock_cli.py` during dev |
| Unix socket | `/run/airlockd.sock` (system install); `/tmp/airlockd-$USER.sock` (dev) |
| HTTP listen | `127.0.0.1:8447` |
| State dir | `/var/lib/airlockd/` (system); `~/.local/state/airlock/` (dev) |
| State file | `state.json` (atomic-write) |
| Event log | `events.jsonl` (append-only, ring-trimmed to 50 MB) |
| Config dir | `/etc/airlockd/` (system); `~/.config/airlock/` (dev) |
| App registry | `<config_dir>/apps/<name>.yaml` (one file per registered app) |
| Daemon log | `/var/log/airlockd.log` (system); `<state_dir>/daemon.log` (dev) |
| systemd unit | `/etc/systemd/system/airlockd.service` |

During development everything runs as the developer's user out of `~/airlock/`.
Promotion to system install is a separate operator-run step (`./install.sh`).

---

## 3. The three operating modes

System-wide, one active at a time. Switched live via `airlock mode <name>`. No
restart needed.

### 3.1 `exclusive`

One lease total, at any time. New requests queue regardless of VRAM budget.
Useful for "I want clean total control for a focused session." Priority is
the queue tiebreaker among waiters.

### 3.2 `priority` (default)

Multiple leases can coexist as long as the sum of declared budgets + safety
margin fits in total VRAM.

- A new request with priority **higher** than at least one active lease, that
  cannot fit alongside everything: triggers preemption of lower-priority
  lease(s) until it fits.
- A new request that fits within current headroom: granted immediately.
- A new request that doesn't fit and isn't high enough to preempt: queued in
  priority-then-FCFS order.

### 3.3 `equal`

(Renamed from `fair-share`.) Multiple leases coexist within VRAM budget;
priority is ignored entirely. All requests served strict FCFS, no preemption.
A new request that doesn't fit waits until something releases naturally.

### 3.4 Priority boost (orthogonal to mode)

`airlock priority <app> <value> [--ttl 30m]` temporarily overrides an app's
default priority for a window. Decays back to the registry value at TTL
expiry. Works in `priority` mode only — no-op in `exclusive` or `equal`.

---

## 4. The reservation model

### 4.1 What a lease is

A reservation of N MiB of VRAM for a specific app, optionally with a TTL.
Identified by an opaque `lease_id`. Held by exactly one process (`client_pid`)
which the broker watches via `kill -0` polling at 2s cadence — when the PID
disappears, the lease auto-releases.

Lease fields:

```
{
  "id":              "lease_01HXYZ8KAQB7",
  "app":             "comfyui",
  "tenant":          "tenant_label",
  "client_pid":      1601234,
  "vram_budget_mib": 14000,
  "vram_actual_mib": 11842,        // most recent nvidia-smi sample
  "priority":        60,
  "acquired_at":     "2026-05-23T07:12:04Z",
  "ttl_s":           null,
  "expires_at":      null,
  "reason":          "txt2img",
  "preempt_handler": {"type": "sigterm"} | {"type": "http", "url": "..."},
  "state":           "active" | "preempting"
}
```

A queued request is the same shape minus the PID/acquired info, plus
`submitted_at`, `queue_position`, `blocking: bool`, `wait_timeout_s`.

### 4.2 The "never OOM" guarantee — exactly what we promise

**Strict guarantee** (for cooperative consumers): if an app uses one of the
three cooperation patterns below (custom-app HTTP, `airlock run` wrapper, or
registered daemon mode), the broker will never let it OOM another cooperative
app. The math is hard:

```
sum(active_lease_budgets) + new_request_budget + safety_margin <= total_vram
```

Requests that would break this math wait. Period.

**Best-effort guarantee** (for unmanaged processes): the broker observes
nvidia-smi continuously and creates implicit leases for processes it can
identify (see §6). Other apps' decisions adapt around the observed usage.
But a naive `python -c "torch.zeros(...)"` that allocates 24 GiB instantly
on a saturated GPU will still OOM — the broker can't prevent allocations it
isn't asked about.

The mitigation for the best-effort case is in §6: register the app's cmdline
pattern so the broker preempts other leases the instant it sees the
unmanaged process start using VRAM. Combined with apps' typical
incremental-allocation behavior (most ML frameworks alloc weights first,
then context), this catches the realistic cases.

### 4.3 Budget enforcement

Every 2s, the broker reads per-PID VRAM from `nvidia-smi --query-compute-apps`.
For each active lease:

- `actual <= budget`: green. Update `vram_actual_mib`.
- `actual > budget`: yellow. Log a warning. If `enforce_budget_strict`
  is true for this app (config), the broker auto-expands the budget to
  `actual + 10%` if headroom permits, else preempts a lower-priority lease
  to make room, else notifies operator.
- `actual > budget * 1.5` sustained 10s and no headroom available: red.
  Broker SIGTERMs the offender unless app config says
  `enforce_budget_strict: false`. Default off — silent expansion.

This is the safety belt. Apps declaring conservative budgets and using more
is normal; the broker accommodates.

### 4.4 Preemption flow

When the broker decides to preempt lease X to satisfy higher-priority
request Y:

1. Mark X as `state: preempting`.
2. Fire X's `preempt_handler`:
   - `sigterm`: SIGTERM the `client_pid`, wait `preempt_grace_s` (default 30s)
     for the process to release VRAM (verified via `nvidia-smi` sample).
   - `http`: POST to the configured URL with `{lease_id, deadline_s}`. Wait
     `preempt_grace_s` for the handler-side hook to release VRAM. If still
     resident at deadline, escalate to SIGTERM.
3. Once X's VRAM is freed (sampled <512 MiB), the broker formally releases X
   (`state: released`, archived to history) and grants Y.
4. If X never frees within total escalation budget (30s + 10s SIGKILL = 40s),
   broker SIGKILLs and proceeds. Logged loudly.

Two leases of equal priority never preempt each other.

### 4.5 Crash + restart semantics

- **Atomic state writes**: state.json is written to `state.json.tmp` then
  `rename()` — no partial states.
- **Daemon restart**: replays state.json, then for each active lease checks
  `kill -0 client_pid`. Dead PIDs → released. Queue carried as-is, paused
  briefly while broker re-samples nvidia-smi, then resumed.
- **Client crash (lease holder)**: PID watchdog catches within 2s, releases.
- **Wrapper crash with subprocess alive**: subprocess orphaned to init.
  Broker sees its VRAM in the next nvidia-smi sample but doesn't see the
  wrapper PID → flags as unmanaged. Operator action (`airlock kill <pid>` or
  manual).
- **Power loss**: state.json on disk, picks up cleanly. Active leases marked
  for revalidation; if PIDs gone (typical), released. Queue intact.
- **History**: every grant/release/preempt/error appended to events.jsonl
  forever. Rotation: when file > 50 MB, rotate to `events.jsonl.1` and start
  fresh. Two rotations kept.

---

## 5. API reference

Two transports, identical paths and bodies:

- **Unix socket**: `/run/airlockd.sock` (system) or `/tmp/airlockd-$USER.sock` (dev). Default for CLI + same-host clients.
- **HTTP**: `127.0.0.1:8447`. For browser UIs and MyApp.

All responses are JSON. Errors return `{ "error": "<machine_code>", "message": "<human>" }` with HTTP 4xx/5xx.

### 5.1 Leases

#### `POST /lease`

Request a lease. Body:

```json
{
  "app":             "comfyui",        // required; should be a registered app name
  "vram_budget_mib": 14000,            // required (unless app registry has default)
  "priority":        60,               // optional; default from registry, else 50
  "ttl_s":           null,             // optional; null = until released
  "blocking":        true,             // default true: long-poll until granted
  "wait_timeout_s":  300,              // default 300s (5 min) when blocking
  "reason":          "txt2img batch",  // optional, for history
  "client_pid":      1601234,          // optional; default = peer PID via SO_PEERCRED on UDS, or required for HTTP
  "preempt_handler": null              // optional override of registry value
}
```

Responses:

- `200 OK { lease_id, granted_at, vram_budget_mib, expires_at }` — granted
- `202 Accepted { request_id, queue_position, eta_s, denial_reason: null }` — queued (only when `blocking: false`)
- `408 Request Timeout` — blocking request timed out waiting
- `409 Conflict { error: "cannot_fit", message: "..." }` — would never fit (budget > total - margin)
- `403 Forbidden { error: "exclusive_mode_held", message: "..." }` — mode=exclusive and another lease is active

#### `DELETE /lease/<id>`

Release. Idempotent. Returns `{ released: true }`.

#### `GET /lease/<id>`

Returns the lease object (active or queued).

#### `GET /leases`

Returns `{ active: [...], queued: [...] }`. Optional `?app=NAME` filter.

#### `POST /lease/<id>/extend`

Body `{ ttl_s: 600 }` — extends or sets TTL. Returns updated lease.

#### `POST /lease/<id>/preempt`

Manual preemption. Body `{ reason: "operator override" }`. Same flow as automatic preemption.

### 5.2 System

#### `GET /status`

```json
{
  "mode": "priority",
  "gpu": {
    "total_mib": 24576,
    "used_mib": 19842,
    "free_mib": 4734,
    "utilization_pct": 73
  },
  "committed_mib": 22500,
  "safety_margin_mib": 512,
  "active_lease_count": 1,
  "queue_depth": 2,
  "unmanaged_processes": [
    {"pid": 14721, "cmdline": "python jupyter-kernel", "vram_mib": 320}
  ]
}
```

#### `POST /mode`

Body `{ "mode": "exclusive" | "priority" | "equal" }`. Returns
`{ mode, applied_at, side_effects: { evicted_leases: [...] } }`. Switching
to `exclusive` while >1 lease is active preempts all but the highest-priority
holder.

#### `GET /history?limit=100&since=<iso>`

Returns recent events.

#### `GET /healthz`

`{ ok: true, started_at, version }`.

### 5.3 Apps

#### `POST /app` (register or update)

```json
{
  "name":              "comfyui",
  "default_budget_mib": 14000,
  "default_priority":   60,
  "preempt_handler":    {"type": "sigterm"},
  "cmdline_match":      "python.*ComfyUI/main.py",
  "watch_implicit":     true,         // if true, broker creates implicit leases when observed
  "enforce_budget_strict": false,
  "launch":             null          // optional: {user, command, env} for `airlock start <app>`
}
```

Returns the stored app record.

#### `GET /apps` — list all registered apps.
#### `DELETE /app/<name>` — unregister.

### 5.4 Priority boost

#### `POST /priority`

```json
{ "app": "comfyui", "priority": 95, "ttl_s": 1800 }
```

Overrides the app's effective priority for `ttl_s` seconds. Returns `{ boosted: true, effective_until }`.

### 5.5 Notifications (optional, registered per-app)

Some apps want to know when their queued lease lands.

#### `GET /lease/<id>/notify?webhook=<url>` — register a one-shot webhook fired on grant.

Used by the `airlock run` wrapper when the user passes `--background`.

---

## 6. Cooperation patterns

Three patterns, in increasing levels of integration:

### Pattern A: Native broker-aware app

The app calls `POST /lease` itself at the moment it's about to do GPU work.
It supplies a `preempt_handler` so the broker can ask it nicely to release.

Example (MyApp's autonomy loop):

```python
async def heartbeat_tick():
    lease = await airlock.acquire(
        app="myagent-worker",
        vram_budget_mib=22500,
        priority=30,
        reason="qwen2.5:32b heartbeat",
        preempt_handler={"type": "http", "url": "http://127.0.0.1:8000/agents/supervisor/pause"},
    )
    try:
        await run_ollama_inference(...)
    finally:
        await airlock.release(lease.id)
```

When something higher-priority asks (e.g., ComfyUI), broker calls MyApp's
pause URL; MyApp pauses its loop and unloads Ollama; broker confirms via
nvidia-smi; ComfyUI proceeds.

### Pattern B: Foreign app, wrapped at launch

For apps we didn't write but launch ourselves:

```bash
airlock run --app comfyui --budget 14g --priority 60 -- python ComfyUI/main.py --port 8188
```

The wrapper acquires the lease, sets `AIRLOCK_LEASE_ID` in the env, execs
the subprocess. Watches the subprocess; on exit, releases. If broker
preempts, sends SIGTERM to the subprocess (configurable).

Variant: `airlock start comfyui` for apps registered as daemons (`launch:` field
set). Broker launches them.

### Pattern C: Foreign app, observation only (no wrapper)

For apps that just get launched without going through `airlock run`:

Broker continuously polls `nvidia-smi --query-compute-apps`. When a new PID
appears, it reads `/proc/<pid>/cmdline` and checks against every registered
app's `cmdline_match` regex.

- **Match found**: create an **implicit lease** for that PID with the app's
  registered defaults. Surfaces in `airlock list` as `(implicit)`. If the
  implicit allocation would push past committed budget, broker preempts
  lower-priority leases reactively. (Caveat: if the observed app allocates
  faster than the broker can preempt, OOM is possible. Most apps allocate
  incrementally, so this works in practice.)
- **No match**: create an **unmanaged-process record** at priority 0,
  budget = currently observed VRAM (updated each poll). Visible in `/status`
  and the UI; no preemption power over it, but its VRAM is debited from
  total when computing headroom.

**The key insight**: Pattern C means you don't actually have to use
`airlock run` for things to mostly work. You only NEED to use `airlock run` (or
custom integration) when you want a **pre-allocated guarantee** — i.e.,
"don't let me OOM at launch time." Operations that can tolerate occasional
OOMs (interactive shells, experiments, jupyter cells) can just run as
normal; the broker observes them, accounts for them, and (if registered) can
even preempt around them.

This addresses the "do all apps need a wrapper?" question with: **no, only
if they want strict guarantees.**

### 6.1 Why not LD_PRELOAD or eBPF interception?

I considered shimming `cuMemAlloc` via `LD_PRELOAD` so that any CUDA app
would block on the broker before allocating. Decided against for v1:

- Fragile across CUDA versions
- Breaks debug-ability (every CUDA app now goes through extra code)
- Hard to test cleanly
- Better to ship a working observation-based system first and add a shim
  later if Pattern C's gaps actually bite

eBPF interception on ioctl() to /dev/nvidia* would work but is overkill for
a single-host single-GPU situation.

Decision: stick with the three-pattern model. Document Pattern C's limits
clearly. Add LD_PRELOAD retry-on-OOM as v2 if needed.

### 6.2 The implicit-lease mechanism — exactly how it works

The broker's nvidia-smi loop is the engine:

```
every 2s:
  procs = nvidia_smi_query_compute_apps()    # [(pid, used_mib), ...]
  for pid, used_mib in procs:
    if pid in known_lease_pids:
      update_actual(lease_for_pid[pid], used_mib)
      continue
    # New process, never seen
    cmdline = read_proc_cmdline(pid)
    matched_app = find_app_with_matching_cmdline(cmdline)
    if matched_app and matched_app.watch_implicit:
      create_implicit_lease(matched_app, pid, used_mib)
      if implicit_lease_pushes_over_budget():
        schedule_preemption_to_make_room(matched_app.priority)
    else:
      create_or_update_unmanaged_record(pid, cmdline, used_mib)

  for lease in active_leases:
    if not pid_alive(lease.client_pid):
      release(lease)
```

The two-second polling cadence is the worst-case latency between an app
starting to allocate and the broker preempting. ComfyUI's initial model
load is hundreds of milliseconds at minimum (often seconds), so this works.
For tighter cases, the `airlock run` wrapper is the answer.

---

## 7. The `gpu` CLI

```
airlock status                       # one-line summary
airlock list                         # active leases + queue + unmanaged
airlock claim --app X --budget 14g [--priority N] [--ttl 30m]
airlock release <lease_id>
airlock run --app X [--budget 14g] -- <cmd>      # wrapper
airlock start <app>                  # for registered daemon-mode apps
airlock stop <app>                   # graceful stop of a managed daemon
airlock kill <pid>                   # SIGTERM a PID; warn if it's a lease holder
airlock mode [exclusive|priority|equal]   # get or set
airlock priority <app> <N> [--ttl 30m]
airlock apps                         # list registered apps
airlock app register --name X --file <yaml>
airlock app remove <name>
airlock history [-n 50]
airlock watch                        # live tail of state changes (uses /history SSE)
```

`airlock claim --background` returns immediately with the lease ID and a
note that the wrapped command should set `AIRLOCK_LEASE_ID` to that value.
Useful for shell scripts that want to interleave broker work with manual
control.

All commands prefer the unix socket; pass `--http URL` to talk to a remote
broker (future: another tenant's broker, etc).

---

## 8. UI: standalone or panel?

Three choices for the UI surface, depending on whether an existing system
panel/dashboard exists to integrate with:

### Choice A: Standalone WebUI on `:8447/ui` (deferred)

A small single-page app served by the daemon itself. Static HTML/JS, talks
to the same HTTP API. Always-on, runs even when nothing else does.

### Choice B: Defer entirely; panel integrates later (recommended for v1)

The daemon's HTTP API is already a clean surface. Whatever the panel CLI
tool is, it can wrap any of the endpoints in §5. We ship the daemon + CLI;
the operator (or panel) builds whatever UI they want on top.

### Choice C: MyApp integrates a per-app management UI (planned for v1)

MyApp's Settings already has the "Autonomy & GPU" section. We extend it
to:

- Show which broker lease(s) MyApp currently holds, queue position when
  blocked
- "Reserve GPU exclusively" button (acquires a `manual-hold` lease
  for some TTL — preempts everything else)
- "Boost priority" slider
- "Release my leases" button

MyApp doesn't show the global broker state in detail (that's the panel's
job); it only shows what *it* is doing. Other custom apps follow the same
pattern: each shows its own footprint.

**Decision for v1**: B + C. Build the daemon + CLI now. MyApp gets its
own per-app UI. Standalone webUI postponed until either the panel is
unavailable in practice or someone really wants it.

---

## 9. LLM usage guide

How an LLM (MyApp's overseer agent, future another tenant agents, etc.) should
think about and use the broker:

### 9.1 The model

For an LLM driving an autonomous workflow:

- Treat the GPU as a shared resource. Before doing any local Ollama
  inference, acquire a lease.
- Be honest about budget. Declare the model size + KV cache estimate, not
  a wishful low number. If you say 8 GiB and use 22 GiB, you'll trigger
  Pillar-2 enforcement.
- Hold leases as briefly as practical. If your tick is "load qwen, do one
  generation, idle 5 min, repeat," consider holding the lease only during
  the generation window, not the idle minutes. (Trade-off: reloading qwen
  is ~20s. Worth it only if contention is real.)
- Always release on tick exit, including error paths.
- Respect preempt handlers. If the broker pauses your supervisor, gracefully
  unload the model and yield. Don't fight it.

### 9.2 Concrete LLM-facing tool definitions

For Letta agents (and the like) that get function-call tools, the broker
should expose these via MyApp's tool layer:

```python
# Existing in myapp/backend/services/letta_tools/
def gpu_acquire(app: str, budget_mib: int, priority: int = 50, reason: str = "") -> dict:
    """Reserve VRAM. Blocks until granted (max 5 min). Returns {lease_id}."""

def gpu_release(lease_id: str) -> dict:
    """Release a previously-acquired lease."""

def gpu_status() -> dict:
    """Get current GPU state: total_mib, used_mib, active leases, queue depth."""
```

The agent's persona explains when to use each. MyApp's heartbeat
supervisor (the host-side runtime) wraps every Ollama call in
`gpu_acquire` → `try: ... finally: gpu_release()` automatically — the agent
itself only needs to think about it when doing explicitly-scoped work.

### 9.3 Anti-patterns to bake into agent prompts

Add to `~/myapp/shared/learnings.md`:

- No allocating without a lease. If `gpu_status` shows your usage but no
  matching lease, immediately call `gpu_acquire` retroactively (broker
  will accept the late lease via the implicit-lease mechanism).
- No setting `priority > 80` unless explicitly authorized by operator — the
  human gets the high end of the scale.
- Don't hold a lease across long idle periods (>10 min) unless registered
  as a `keepalive` workload.

---

## 10. MyApp integration (v1)

Concrete changes to the existing MyApp codebase:

### 10.1 Backend

- New file `~/myapp/backend/services/airlock_client.py` — async HTTP
  client for the broker's API. Methods: `acquire`, `release`, `status`,
  `set_priority`. Reads `AIRLOCK_URL` env (default `http://127.0.0.1:8447`).
- `services/supervisor.py` — every Ollama call goes through `with
  await broker.acquire_context("myagent-worker", 22500, priority=30)`.
- `routes/autonomy.py` — the existing `/agents/supervisor/pause` URL becomes
  the MyApp side of the broker's preempt handler. (Already a working
  endpoint; we just register the URL with the broker on backend startup.)
- New route `routes/autonomy.py` additions:
  - `GET /gpu/broker/status` — proxy to broker, augmented with MyApp's
    own leases highlighted
  - `POST /gpu/broker/reserve` — body `{ duration_s, reason }` — MyApp
    acquires a manual exclusive lease for the operator
  - `POST /gpu/broker/release-mine` — release any MyApp-held leases
  - `POST /gpu/broker/boost` — body `{ priority, ttl_s }` — promote
    MyApp's default priority temporarily

The local "stop all" button I shipped earlier becomes a thin wrapper around
the broker calls: stop all = pause supervisor + disable cron + release any
MyApp leases. (No more direct Ollama unload — broker handles that as a
side effect of release.)

### 10.2 Frontend

Settings → Autonomy & GPU section gets two new rows:

| Row | Shows | Action |
|---|---|---|
| broker | "broker up · 1 of my leases · q-depth 0" | (link to panel later) |
| my leases | "myagent-worker (22.5 GiB, held 4m, pri 30)" | release · boost |

Plus a "Reserve GPU for me" button that calls `POST /gpu/broker/reserve` with
a 30-min default — used when the operator wants the autonomy stack to
yield for human work without launching a specific app.

### 10.3 Migration order

1. Ship broker daemon + CLI standalone (no MyApp change).
2. Register `myagent-worker` as a broker app via `airlock app register`.
3. Wire `airlock_client.py` and switch supervisor to use it. Old direct
   off-switch becomes a thin wrapper around the broker.
4. Update Settings UI.

Each step is independent. The MyApp side can be deferred without
breaking anything — the broker just sees MyApp's Ollama traffic as
unmanaged (and works around it).

---

## 11. Other custom-app integrations (forward-looking)

Same shape as MyApp: each custom app embeds the small `airlock_client`
in whatever language, exposes its own per-app UI (reserve / boost /
release), and registers itself with the broker on startup with a preempt
handler.

Anticipated future custom apps that'll need this:
- another tenant autonomy stack (mirror of MyApp)
- training/finetuning experiments (run via `airlock run` for ad-hoc, or built
  as registered apps for repeatability)
- a future "image generation" custom app (different from raw ComfyUI)

Each one is ~50 LOC of client glue.

---

## 12. State + history exhibit

Everything important on disk, all human-inspectable:

```
~/.local/state/airlock/
├── state.json           # current leases + queue + mode (atomic-write)
├── events.jsonl         # forever event log, rotated at 50MB
├── events.jsonl.1       # previous rotation
├── daemon.log           # daemon's own log
└── boost_overrides.json # active priority boosts (TTL'd)

~/.config/airlock/
├── config.yaml          # global: mode default, safety margin, polling rates
└── apps/
    ├── comfyui.yaml
    ├── myagent-worker.yaml
    └── jupyter-kernel.yaml
```

`cat state.json | jq` gives you the full picture at any time. No
database, no daemons to query for basic facts.

---

## 13. Testing strategy

Three layers:

### 13.1 Unit tests (`tests/unit/`)

Pure-Python tests of the lease-math and queue-state machine. No daemon
spawn, no GPU touched. Cover:

- All three modes' grant/deny/queue decisions
- Preemption ordering by priority + FCFS tiebreak
- Budget math with safety margin
- Boost TTL expiry
- State serialization round-trip

### 13.2 Integration tests (`tests/integration/`)

Spawn the daemon as a subprocess (with state dir under tmpdir, mocked
nvidia-smi via a stub binary on PATH), exercise via the real HTTP API:

- Acquire → release flow
- Blocking acquire that fires after release
- Preempt sequence end-to-end (mocked client process)
- State persistence across daemon restart
- PID watchdog releases dead client
- Implicit-lease creation from a fake "new GPU process" event

### 13.3 End-to-end smoke (`tests/e2e/`)

Real daemon, real nvidia-smi, a real harmless GPU consumer (a tiny
`torch.empty(100, 100).cuda()` script) — actual VRAM allocation + release
observed. Run manually, not in CI.

---

## 14. Install / promote to system

Two scripts:

- `install-dev.sh` — sets up under `~/.local/state/airlock/` + `~/.config/airlock/`, launches daemon under user. No sudo. **Default for development.**
- `install-system.sh` — operator-run with sudo. Copies daemon to `/usr/local/bin/`, CLI to `/usr/local/bin/`, creates `airlock` system user, sets up systemd unit, migrates state from dev location if present.

The daemon is identical in both cases — only paths and ownership differ.

---

## 14.1 Cross-user enforcement — a v1 reality

When running the broker as a non-root user (typical dev mode),
SIGTERM/SIGKILL against another user's processes (e.g., ComfyUI as
another user) is blocked by Unix permissions. The broker correctly issues the
signal but the kernel rejects it; the offending process keeps running. The
broker still *thinks* the lease was released after grace, which can cause
double-commit on the GPU (broker grants the queued request even though VRAM
isn't actually free).

**Mitigation for v1 dev**: only register apps owned by the same user as the
broker. ComfyUI registered under a broker running as a non-owning user will be *observed* (in
unmanaged/implicit-lease state) but cannot be effectively *preempted*.

**Fix in system install** (`install-system.sh`): the broker runs as
root or as a dedicated `airlock` system user with `CAP_KILL` (or by being
in a shared group). At that point SIGTERM/SIGKILL crosses user boundaries
and enforcement is symmetric.

For HTTP preempt handlers (Pattern A), this isn't an issue — MyApp's
preempt URL is reachable from anywhere on the host, so the broker calls
MyApp and MyApp pauses itself.

## 14.2 MyApp integration scope shipped in v1

MyApp's backend exposes broker-proxy routes (`/gpu/broker/status`,
`/gpu/broker/reserve`, `/gpu/broker/release-mine`, `/gpu/broker/boost`,
`/gpu/broker/register`). Settings UI gets a new "broker" row showing
connection state, a "my leases" row, and operator-hold + boost buttons.

**Not yet shipped**: MyApp's supervisor (`services/supervisor.py`)
still calls Ollama directly without acquiring a broker lease first. So
MyApp's per-tick Ollama load is invisible to the broker except via the
implicit-lease mechanism (when the broker observes the ollama runner PID
and matches against the `myagent-worker` cmdline regex). The follow-on
work is to wrap each supervisor tick in `broker.acquire(...)` / `release(...)`
— estimated 30 LOC change.

This deferral is intentional: it makes the broker integration additive
(broker can come and go without breaking MyApp) and lets the operator
validate the broker behavior in production before tightly coupling
MyApp's heartbeat loop to it.

## 15. Open items / explicit non-goals

- **Web UI**: deferred to panel integration. If panel doesn't materialize in
  a reasonable timeframe, build a minimal HTML page.
- **Multi-GPU**: out of scope; trivial extension when needed.
- **Remote broker** (broker on host X serving consumers on host Y):
  not needed; revisit if it ever is.
- **Cross-host coordination** (broker on host A talking to broker on host B):
  not needed since they're on the same physical host with one GPU. Both
  tenants will use the same broker instance.
- **Auth/permissions**: none. Single operator.
- **Detailed GPU-utilization scheduling**: only manages VRAM residency, not
  compute time. The GPU itself serializes compute kernels.

---

## 16. Summary (the things you'd ask back about)

- **One Python daemon**, stdlib only, single-file install. Listens on unix
  socket + 127.0.0.1:8447 HTTP.
- **Three modes**: exclusive / priority (default) / equal. Switchable live.
- **Priority boost** is orthogonal to mode.
- **Three cooperation patterns**: native HTTP API, `airlock run` wrapper,
  observation-with-implicit-leases. No wrapper required for things to "mostly
  work"; required for strict no-OOM guarantee.
- **`gpu` CLI** as the primary interface for humans + scripts.
- **State on disk**, atomic writes, survives daemon and machine crashes.
- **MyApp integrates as the first client**, gets its own per-app UI for
  reserve/boost/release. Doesn't try to be the global panel.
- **Other custom apps follow MyApp's pattern**: small client lib, their
  own per-app UI plugged into the same API.
- **WebUI deferred** to panel integration; daemon's HTTP API is the
  integration point. Operator can decide later.
- **ollama-bgm container** removed (operator confirmed it's done).
- **First implementation lives under `~/airlock/`**, runnable as a regular user,
  no sudo needed. Promotion to system daemon is a separate operator step.
