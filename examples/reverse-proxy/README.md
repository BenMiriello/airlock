# reverse-proxy fallback

A minimal HTTP reverse proxy that intercepts ComfyUI / Forge generation
requests and wraps them in an airlock lease. Use this when the drop-in
extensions (`examples/comfyui-airlock/`, `examples/airlock-forge/`) aren't
viable — e.g., you don't control the app install but you do control the
port.

## When to use what

| Situation | Use |
|---|---|
| You control the ComfyUI install | drop in `examples/comfyui-airlock/` |
| You control the Forge install | drop in `examples/airlock-forge/` |
| You don't control either, but you control the network | this proxy |
| You can't deploy anything | nothing — observation-mode only |

The proxy is strictly weaker than the in-process extensions:
- ~5–10 ms latency overhead per request
- WebSocket `/ws` (live progress) is NOT proxied in this minimal version;
  clients lose live progress updates but generations still complete
- Can only acquire a lease per HTTP request — can't see UI clicks that
  bypass the API

## Usage

```bash
# ComfyUI on :8188 → proxy on :8189
python3 airlock_proxy.py \
    --backend comfyui \
    --upstream http://127.0.0.1:8188 \
    --listen 0.0.0.0:8189 \
    --app comfyui --budget 14g

# Forge on :7860 → proxy on :7861
python3 airlock_proxy.py \
    --backend forge \
    --upstream http://127.0.0.1:7860 \
    --listen 0.0.0.0:7861 \
    --app forge --budget 10g
```

Point your clients at the proxy port instead of the upstream port.

## Fail-open

If airlockd is unreachable, the proxy logs and forwards without acquiring a
lease — clients still get a working app, just without the broker layer.
