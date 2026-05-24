# comfyui-airlock

A ComfyUI custom-node that integrates ComfyUI with the airlock GPU broker.

**Drop-in.** Copy this directory into your ComfyUI install:

```
cp -r comfyui-airlock /path/to/ComfyUI/custom_nodes/
```

Restart ComfyUI. That's it. No flag changes, no other config.

## What it does

- On ComfyUI startup, registers `comfyui` as an app with airlock (with
  `preempt_handler: comfyui_free`) so airlock can release ComfyUI's VRAM
  without killing the process.
- Before every prompt, acquires a lease from airlock sized for the workflow's
  declared model footprint (with a sensible fallback if the workflow doesn't
  declare).
- Releases the lease when the prompt completes (or errors out).
- On preempt from airlock: drops models via
  `model_management.unload_all_models()` + `soft_empty_cache()`. ComfyUI keeps
  running, just with no models loaded; next prompt re-loads.

## Configuration

Optional env vars (set before launching ComfyUI):

| Var | Default | Meaning |
|---|---|---|
| `AIRLOCK_URL` | `http://127.0.0.1:8447` | airlockd base URL |
| `AIRLOCK_APP_NAME` | `comfyui` | app name to register as |
| `AIRLOCK_DEFAULT_BUDGET_MIB` | `14336` | budget when workflow doesn't declare |
| `AIRLOCK_PRIORITY` | `60` | default priority |
| `AIRLOCK_DISABLED` | unset | set to `1` to disable the integration entirely |

## What if airlock is down?

Every airlock call has a short timeout and a quiet failure path. If airlockd
isn't reachable, ComfyUI runs normally — generations proceed without
reservations. The integration is **fail-open**: airlock down ≠ ComfyUI broken.

## Recommended ComfyUI launch flags

For airlock's bookkeeping to stay tight, run ComfyUI with:

```
python main.py --listen --port 8188 --disable-smart-memory --reserve-vram 1
```

- `--disable-smart-memory` makes ComfyUI unload between prompts, so the
  acquire/release pattern matches actual VRAM behavior
- `--reserve-vram 1` leaves 1 GiB headroom for other apps

These are optional but make airlock + ComfyUI play nicely together.
