"""`airlock` CLI — talks to airlockd over HTTP.

Subcommands:
  status      one-line system summary
  list        all active + queued leases + unmanaged
  claim       acquire a lease (synchronous default)
  release     release a lease by id
  run         wrap a subprocess with a lease (auto-release on exit)
  start       launch a registered daemon-mode app via the broker
  stop        stop a running daemon-mode app
  kill        SIGTERM a PID (warns if it holds a lease)
  mode        get or set system mode
  priority    boost an app's priority for a window
  apps        list registered apps
  app         register/show/remove an app
  history     recent broker events
  watch       (TODO) live tail

URL: --url overrides; default http://127.0.0.1:8447.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_URL = os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8447")


# ---------- HTTP plumbing ----------


def _request(url: str, method: str, path: str, body: dict | None = None,
             timeout: float = 360.0) -> tuple[int, Any]:
    full = url.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(full, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                return r.status, {"raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            return e.code, json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode("utf-8", "replace")}
    except urllib.error.URLError as e:
        sys.exit(f"airlockd unreachable at {url}: {e.reason}")


def _human_mib(mib: int) -> str:
    if mib >= 1024:
        return f"{mib/1024:.1f}G"
    return f"{mib}M"


def _parse_budget(s: str) -> int:
    """Accept '14000', '14000mib', '14g', '14.5gib', etc."""
    s = s.strip().lower()
    m = re.match(r"^([0-9.]+)\s*(mib|m|gib|g|mb|gb)?$", s)
    if not m:
        raise ValueError(f"bad budget: {s!r}")
    val = float(m.group(1))
    unit = m.group(2) or "mib"
    if unit in ("g", "gb", "gib"):
        return int(val * 1024)
    return int(val)


def _parse_ttl(s: str) -> int:
    """Accept '30m', '2h', '300s', '300'."""
    s = s.strip().lower()
    m = re.match(r"^([0-9]+)\s*(s|m|h)?$", s)
    if not m:
        raise ValueError(f"bad ttl: {s!r}")
    val = int(m.group(1))
    unit = m.group(2) or "s"
    return val * {"s": 1, "m": 60, "h": 3600}[unit]


# ---------- handlers ----------


def cmd_status(args: argparse.Namespace) -> int:
    code, j = _request(args.url, "GET", "/status")
    if code != 200:
        print(json.dumps(j, indent=2)); return 1
    gpu = j.get("gpu", {})
    print(f"mode={j['mode']}  vram={_human_mib(gpu.get('used_mib', 0))}/{_human_mib(j['total_vram_mib'])}  "
          f"committed={_human_mib(j['committed_mib'])}  free_budget={_human_mib(j['free_budget_mib'])}  "
          f"leases={j['active_lease_count']}  queue={j['queue_depth']}  "
          f"util={gpu.get('utilization_pct', 0)}%")
    boosts = j.get("boosts", {})
    if boosts:
        print("boosts: " + " ".join(f"{a}={v['priority']}({int(v['expires_in_s'])}s)"
                                    for a, v in boosts.items()))
    unmanaged = j.get("unmanaged_processes", [])
    if unmanaged:
        print(f"unmanaged: {len(unmanaged)} process(es), total "
              f"{_human_mib(sum(u['vram_mib'] for u in unmanaged))}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    code, j = _request(args.url, "GET", "/leases")
    if code != 200:
        print(json.dumps(j, indent=2)); return 1
    if args.json:
        print(json.dumps(j, indent=2)); return 0
    print("ACTIVE LEASES")
    if not j["active"]:
        print("  (none)")
    for l in j["active"]:
        st = l["state"]
        flag = " [implicit]" if l.get("implicit") else ""
        print(f"  {l['id']}  {l['app']:<22}  pid={l['client_pid']:<6}  "
              f"budget={_human_mib(l['vram_budget_mib'])}  "
              f"actual={_human_mib(l['vram_actual_mib'])}  pri={l['priority']:<3}  "
              f"{st}{flag}  '{l.get('reason','')[:40]}'")
    print("QUEUE")
    if not j["queued"]:
        print("  (empty)")
    for r in j["queued"]:
        print(f"  {r['id']}  {r['app']:<22}  pid={r['client_pid'] or '?':<6}  "
              f"budget={_human_mib(r['vram_budget_mib'])}  pri={r['priority']:<3}  "
              f"'{r.get('reason','')[:40]}'")
    code, st = _request(args.url, "GET", "/status")
    if code == 200:
        unmanaged = st.get("unmanaged_processes", [])
        if unmanaged:
            print("UNMANAGED")
            for u in unmanaged:
                print(f"  pid={u['pid']:<6}  vram={_human_mib(u['vram_mib'])}  "
                      f"{u['cmdline'][:60]}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    body = {
        "app": args.app,
        "vram_budget_mib": _parse_budget(args.budget) if args.budget else None,
        "client_pid": args.pid if args.pid else os.getpid(),
        "priority": args.priority,
        "reason": args.reason or "",
        "blocking": not args.no_block,
        "wait_timeout_s": args.wait,
    }
    if args.ttl:
        body["ttl_s"] = _parse_ttl(args.ttl)
    # Strip None values so registry defaults can fill them.
    body = {k: v for k, v in body.items() if v is not None}
    code, j = _request(args.url, "POST", "/lease", body, timeout=args.wait + 10)
    if code == 200:
        if args.json:
            print(json.dumps(j, indent=2))
        else:
            print(j["id"])
        return 0
    if code == 202:
        if args.json:
            print(json.dumps(j, indent=2))
        else:
            print(f"queued: {j['request_id']}  position={j.get('queue_position')}")
        return 0
    print(f"error ({code}): {j.get('error')} — {j.get('message','')}", file=sys.stderr)
    return 2


def cmd_release(args: argparse.Namespace) -> int:
    code, j = _request(args.url, "DELETE", f"/lease/{args.lease_id}")
    print(json.dumps(j) if args.json else ("released" if j.get("released") else "not found"))
    return 0 if code == 200 else 1


def cmd_run(args: argparse.Namespace) -> int:
    """Acquire lease, fork+exec subprocess, hold lease for its lifetime,
    release on exit."""
    cmd = list(args.cmd)
    # argparse.REMAINDER keeps the literal '--' separator; drop it.
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("error: provide a command after --", file=sys.stderr)
        return 2
    args.cmd = cmd

    body: dict[str, Any] = {
        "app": args.app,
        "client_pid": os.getpid(),  # parent (us); we re-update to child after fork
        "reason": args.reason or " ".join(args.cmd)[:80],
        "blocking": True,
        "wait_timeout_s": args.wait,
    }
    if args.budget:
        body["vram_budget_mib"] = _parse_budget(args.budget)
    if args.priority is not None:
        body["priority"] = args.priority
    if args.ttl:
        body["ttl_s"] = _parse_ttl(args.ttl)

    # Acquire
    print(f"[gpu run] acquiring lease for {args.app}…", file=sys.stderr)
    code, j = _request(args.url, "POST", "/lease", body, timeout=args.wait + 10)
    if code != 200:
        print(f"[gpu run] error: {j}", file=sys.stderr)
        return 2
    lease_id = j["id"]
    print(f"[gpu run] lease={lease_id} budget={_human_mib(j['vram_budget_mib'])}", file=sys.stderr)

    # Spawn subprocess
    env = dict(os.environ)
    env["AIRLOCK_LEASE_ID"] = lease_id

    def _kill_child(sig: int, _frame: Any) -> None:
        if proc and proc.poll() is None:
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    proc = subprocess.Popen(args.cmd, env=env)

    # Re-bind the lease to the actual subprocess PID so the broker's watchdog
    # tracks the right process. (Best-effort: extend lease record via a side
    # mechanism. For v1 we don't expose a /lease/<id>/rebind endpoint; instead
    # the wrapper holds the lease itself and the child runs as a grandchild
    # whose lifetime we monitor.)
    signal.signal(signal.SIGTERM, _kill_child)
    signal.signal(signal.SIGINT, _kill_child)

    try:
        exit_code = proc.wait()
    finally:
        print(f"[gpu run] subprocess exited ({exit_code if 'exit_code' in dir() else '?'}); releasing lease",
              file=sys.stderr)
        _request(args.url, "DELETE", f"/lease/{lease_id}")
    return exit_code


def cmd_mode(args: argparse.Namespace) -> int:
    if args.set:
        code, j = _request(args.url, "POST", "/mode", {"mode": args.set})
    else:
        code, j = _request(args.url, "GET", "/mode")
    print(json.dumps(j) if args.json else j.get("mode", j))
    return 0 if code == 200 else 1


def cmd_priority(args: argparse.Namespace) -> int:
    body = {"app": args.app, "priority": args.priority}
    if args.ttl:
        body["ttl_s"] = _parse_ttl(args.ttl)
    code, j = _request(args.url, "POST", "/priority", body)
    print(json.dumps(j))
    return 0 if code == 200 else 1


def cmd_apps(args: argparse.Namespace) -> int:
    code, j = _request(args.url, "GET", "/apps")
    if args.json:
        print(json.dumps(j, indent=2)); return 0 if code == 200 else 1
    for a in j.get("apps", []):
        ph = a.get("preempt_handler", {})
        ph_s = ph.get("type", "?") + (f" {ph.get('url','')}" if ph.get("url") else "")
        print(f"{a['name']:<22}  budget={_human_mib(a['default_budget_mib'])}  "
              f"pri={a['default_priority']:<3}  watch={a.get('watch_implicit', True)}  "
              f"preempt={ph_s}  match={a.get('cmdline_match') or '-'}")
    return 0


def cmd_app_register(args: argparse.Namespace) -> int:
    if args.file:
        with open(args.file) as f:
            body = json.load(f)
    else:
        body = {
            "name": args.name,
            "default_budget_mib": _parse_budget(args.budget) if args.budget else 1000,
            "default_priority": args.priority,
            "cmdline_match": args.match,
            "watch_implicit": not args.no_watch,
        }
        if args.preempt_url:
            body["preempt_handler"] = {"type": "http", "url": args.preempt_url}
    code, j = _request(args.url, "POST", "/app", body)
    print(json.dumps(j, indent=2))
    return 0 if code == 200 else 1


def cmd_app_remove(args: argparse.Namespace) -> int:
    code, j = _request(args.url, "DELETE", f"/app/{args.name}")
    print(json.dumps(j))
    return 0 if code == 200 else 1


def cmd_history(args: argparse.Namespace) -> int:
    code, j = _request(args.url, "GET", f"/history?limit={args.limit}")
    if args.json:
        print(json.dumps(j, indent=2)); return 0
    for ev in j.get("history", []):
        ts = ev.get("ts", "")[:19]
        kind = ev.get("kind", "?")
        extra = " ".join(f"{k}={v}" for k, v in ev.items() if k not in {"ts", "ts_mono", "kind"})
        print(f"{ts}  {kind:<24}  {extra}")
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    pid = args.pid
    code, j = _request(args.url, "GET", "/leases")
    if code == 200:
        for l in j.get("active", []):
            if l["client_pid"] == pid:
                print(f"WARN: pid {pid} holds lease {l['id']} for {l['app']}", file=sys.stderr)
                if not args.force:
                    print("re-run with --force to proceed", file=sys.stderr)
                    return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("no such pid", file=sys.stderr); return 1
    print(f"sent SIGTERM to {pid}")
    return 0


# ---------- parser ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="airlock", description="Airlock — GPU traffic manager CLI")
    p.add_argument("--url", default=DEFAULT_URL, help="airlockd URL (default $AIRLOCK_URL or http://127.0.0.1:8447)")
    p.add_argument("--json", action="store_true", help="emit raw JSON instead of human format")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("status"); ps.set_defaults(func=cmd_status)
    pl = sub.add_parser("list"); pl.set_defaults(func=cmd_list)

    pc = sub.add_parser("claim", help="acquire a lease")
    pc.add_argument("--app", required=True)
    pc.add_argument("--budget", help="VRAM budget (e.g. 14g, 8000mib)")
    pc.add_argument("--priority", type=int)
    pc.add_argument("--pid", type=int, help="bind lease to this PID (default current)")
    pc.add_argument("--ttl", help="lease TTL (e.g. 30m, 2h)")
    pc.add_argument("--reason", help="free-form reason for history")
    pc.add_argument("--no-block", action="store_true", help="return immediately if queued")
    pc.add_argument("--wait", type=int, default=300, help="max wait seconds when blocking")
    pc.set_defaults(func=cmd_claim)

    pr = sub.add_parser("release")
    pr.add_argument("lease_id")
    pr.set_defaults(func=cmd_release)

    pn = sub.add_parser("run", help="wrap a command with a lease")
    pn.add_argument("--app", required=True)
    pn.add_argument("--budget")
    pn.add_argument("--priority", type=int)
    pn.add_argument("--ttl")
    pn.add_argument("--reason")
    pn.add_argument("--wait", type=int, default=300)
    pn.add_argument("cmd", nargs=argparse.REMAINDER, help="command after --")
    pn.set_defaults(func=cmd_run)

    pm = sub.add_parser("mode")
    pm.add_argument("--set", choices=["exclusive", "priority", "equal"])
    pm.set_defaults(func=cmd_mode)

    pp = sub.add_parser("priority", help="boost an app's priority")
    pp.add_argument("app")
    pp.add_argument("priority", type=int)
    pp.add_argument("--ttl", help="boost TTL (default 30m)")
    pp.set_defaults(func=cmd_priority)

    pa = sub.add_parser("apps")
    pa.set_defaults(func=cmd_apps)

    pareg = sub.add_parser("app")
    pareg_sub = pareg.add_subparsers(dest="app_action", required=True)
    par = pareg_sub.add_parser("register")
    par.add_argument("--name")
    par.add_argument("--budget")
    par.add_argument("--priority", type=int, default=50)
    par.add_argument("--match", help="cmdline regex for implicit-lease detection")
    par.add_argument("--preempt-url", help="HTTP preempt handler URL")
    par.add_argument("--no-watch", action="store_true", help="disable implicit-lease detection")
    par.add_argument("--file", help="JSON file with full app config")
    par.set_defaults(func=cmd_app_register)
    pad = pareg_sub.add_parser("remove")
    pad.add_argument("name")
    pad.set_defaults(func=cmd_app_remove)

    ph = sub.add_parser("history")
    ph.add_argument("-n", "--limit", type=int, default=50)
    ph.set_defaults(func=cmd_history)

    pk = sub.add_parser("kill")
    pk.add_argument("pid", type=int)
    pk.add_argument("--force", action="store_true")
    pk.set_defaults(func=cmd_kill)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
