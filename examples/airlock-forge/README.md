# airlock-forge

A Forge / Stable Diffusion WebUI extension that integrates with the airlock
GPU broker.

**Drop-in.** Copy this directory into your Forge `extensions/` folder:

```
cp -r airlock-forge /path/to/stable-diffusion-webui-forge/extensions/
```

Restart Forge. No other config needed.

## What it does

- On Forge startup, registers `forge` as an app with airlock (with
  `preempt_handler: forge_unload` so airlock can release Forge's VRAM via
  `/sdapi/v1/unload-checkpoint` instead of killing the process).
- Wraps every generation (txt2img, img2img, both via UI and API) via a
  Forge Script: acquires a lease in `process()`, releases in
  `postprocess()`.
- On preempt: Forge's `/sdapi/v1/unload-checkpoint` releases the model
  cleanly; next generation reloads.

## Configuration

Same env vars as the ComfyUI integration:

| Var | Default | Meaning |
|---|---|---|
| `AIRLOCK_URL` | `http://127.0.0.1:8447` | airlockd URL |
| `AIRLOCK_FORGE_APP_NAME` | `forge` | app name to register as |
| `AIRLOCK_FORGE_DEFAULT_BUDGET_MIB` | `10240` | budget per generation |
| `AIRLOCK_FORGE_PRIORITY` | `60` | default priority |
| `AIRLOCK_DISABLED` | unset | set to `1` to disable |

## Fail-open

If airlockd isn't reachable, Forge runs unchanged. Generations proceed without
reservations.

## Recommended Forge launch flags

For airlock + Forge to play nicely:

```
./webui.sh --listen --always-offload-from-vram
```

`--always-offload-from-vram` makes Forge release the VRAM-resident model
between generations, so the acquire/release lease cadence matches reality.
