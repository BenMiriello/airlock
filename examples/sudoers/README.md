# Cross-user signal authority for airlock

When airlock is the SIGTERM/SIGKILL escalator (preempt fallback after the
soft `comfyui_free` / `forge_unload` / `http` handlers fail), it needs
authority to signal processes owned by other users.

Two ways to grant it. Pick one.

## Option 1: `CAP_KILL` via systemd (recommended)

If you're running airlockd from systemd (`examples/systemd/airlockd.service`),
just keep these lines uncommented in the unit:

```
AmbientCapabilities=CAP_KILL
CapabilityBoundingSet=CAP_KILL
```

Kernel grants the daemon's process — and only it — `CAP_KILL`. Audit trail:
systemd's own journal. No sudoers to maintain.

## Option 2: sudoers (if not using systemd)

If airlockd runs as a non-root user without `CAP_KILL` and you can't grant
the capability for some reason, an alternative is a tightly-scoped sudoers
entry.

Drop this file as `/etc/sudoers.d/airlock` (mode 0440):

```
# Allow airlock to SIGTERM/SIGINT processes owned by comfyuser and forgeuser
# for VRAM-preempt escalation. Scope is the minimal set of kill commands;
# arbitrary command execution is NOT permitted.

airlock ALL=(comfyuser,forgeuser) NOPASSWD: /bin/kill -TERM *, /bin/kill -INT *, /bin/kill -KILL *
```

Then change `airlockd`'s preempt code to shell out via:

```
sudo -u comfyuser /bin/kill -TERM <pid>
```

instead of `os.kill(pid, SIGTERM)`. (This isn't wired into airlockd by
default — Option 1 is preferred. Add a `preempt_handler` type `sudo_kill`
if you go this route.)

## What you can skip

If the apps you care about are managed by the in-process extensions and the
`comfyui_free` / `forge_unload` HTTP soft-preempt always succeeds, signal
escalation is never needed and neither of the above is required. The path
matters only for:

- Unmanaged processes (someone runs a CUDA Python script as another user
  without going through airlock)
- Apps whose HTTP server is wedged and can't respond to `/free` /
  `/sdapi/v1/unload-checkpoint`
- Lease holders that ignore preempt-handler webhooks
