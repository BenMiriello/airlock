# systemd units for airlock + managed apps

These are templates. Edit user/paths, then install with:

```sh
sudo cp airlockd.service comfyui.service forge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airlockd
sudo systemctl enable --now comfyui forge
```

## airlockd.service

The broker daemon itself. Runs as a dedicated `airlock` user. Grants
`CAP_KILL` so cross-user SIGTERM works for the preempt escalation path. State
at `/var/lib/airlockd/`, config at `/etc/airlockd/`.

## comfyui.service / forge.service

Wrap ComfyUI and Forge under systemd with `Restart=on-failure`. The
critical property: if airlock (or anything else) kills these apps to recover
from a VRAM crisis, systemd brings them back automatically.

Both units document the recommended airlock-friendly flags:
- ComfyUI: `--disable-smart-memory --reserve-vram 1`
- Forge: `--always-offload-from-vram`

Both set `PYTORCH_ALLOC_CONF=expandable_segments:True` to reduce
fragmentation-related OOMs on 3090-class consumer cards.

The commented-out `LD_PRELOAD` lines enable HAMi-core's hard VRAM cap once
`install-hami.sh` has been run. Uncomment + set the desired
`CUDA_DEVICE_MEMORY_LIMIT` per-process.

## Drop-in extensions

These systemd units assume the airlock drop-in extensions are also
installed:

- `examples/comfyui-airlock/` → `~comfyuser/ComfyUI/custom_nodes/`
- `examples/airlock-forge/` → `~forgeuser/stable-diffusion-webui-forge/extensions/`

The extensions register the app with airlockd on each startup (idempotent),
so no manual `airlock app register` is needed.
