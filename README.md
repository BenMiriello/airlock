# airlock

A small, durable, host-level daemon that owns the single GPU on a multi-app
Linux box. Every consumer — custom apps, foreign apps like ComfyUI, ad-hoc
shell scripts — coordinates through it. The goal is **no OOMs across apps**
and a queryable picture of who's using the card.

Status: **early / single-host**. Built for a single RTX 3090 shared between
a local-LLM autonomy stack, ComfyUI, and ad-hoc work. Cross-host coordination
and multi-GPU are out of scope.

See [`PLAN.md`](PLAN.md) for the full design, API reference, integration
patterns, and rationale.

## Why

One GPU. Multiple workloads that don't fit at the same time. The current
state of the art is "remember to unload one before launching the other."
Airlock turns that into a small, supervised reservation system with three
modes (exclusive / priority / equal) and three cooperation patterns (native
HTTP API, `airlock run` wrapper, observation-with-implicit-leases).

## What it gives you

- A **lease-based reservation broker**. Apps that play by the rules can
  never OOM each other.
- A **queue** with three modes — switchable live, no restart.
- **Priority boosts** orthogonal to mode (e.g., "ComfyUI to top for 30 min").
- **Observation of unmanaged processes** — even apps that don't ask
  permission are surfaced in the UI, accounted for, and (if registered)
  automatically promoted to implicit leases.
- **Crash durability** — atomic state writes, PID watchdog auto-releases
  dead clients, append-only event log.
- **Single Python file daemon, stdlib only.** No dependencies.

## Quick start (dev mode, no sudo)

```bash
# Run the daemon
python3 src/airlockd.py

# In another shell — register an app and claim a lease
python3 src/airlock_cli.py app register --name comfyui --budget 14g --priority 60 --match 'main\.py.*--port 8188'
python3 src/airlock_cli.py status
python3 src/airlock_cli.py list
python3 src/airlock_cli.py claim --app test --budget 1g --pid $$ --reason 'smoke'
python3 src/airlock_cli.py release <lease_id>

# Wrap an arbitrary command with a managed lease
python3 src/airlock_cli.py run --app comfyui -- python ComfyUI/main.py --port 8188
```

Listens on `http://127.0.0.1:8447` by default. State at
`~/.local/state/airlock/`, config at `~/.config/airlock/`. Override via
`AIRLOCK_URL`, `AIRLOCK_STATE_DIR`, `AIRLOCK_CONFIG_DIR`.

## Tests

```bash
python3 -m unittest tests.test_core tests.test_integration
```

## System install

Not yet automated. See `PLAN.md` §14 for the design (`/opt/airlock/`,
dedicated user, systemd unit, `CAP_KILL` for cross-user enforcement).

## License

TBD.
