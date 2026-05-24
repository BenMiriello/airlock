"""Atomic state persistence + append-only event log."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.events_file = self.state_dir / "events.jsonl"
        self.events_max_bytes = 50 * 1024 * 1024  # 50 MB

    def load_state(self) -> dict | None:
        if not self.state_file.exists():
            return None
        try:
            return json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def save_state(self, snap: dict) -> None:
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2))
        os.replace(tmp, self.state_file)

    def log_event(self, kind: str, **fields: Any) -> None:
        rec = {"ts": _now_iso(), "ts_mono": time.monotonic(), "kind": kind, **fields}
        with self.events_file.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        # Rotate if too big.
        try:
            if self.events_file.stat().st_size > self.events_max_bytes:
                rotated = self.events_file.with_suffix(".jsonl.1")
                if rotated.exists():
                    rotated.unlink()
                self.events_file.rename(rotated)
        except OSError:
            pass

    def recent_events(self, limit: int = 100) -> list[dict]:
        if not self.events_file.exists():
            return []
        # Tail the last `limit` lines without loading the whole file.
        lines: list[str] = []
        with self.events_file.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") < limit + 1:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
            lines = data.decode("utf-8", "replace").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


class AppRegistry:
    """Filesystem-backed app registry. One JSON file per app at
    <config_dir>/apps/<name>.json. Loaded into memory at startup; writes go
    to disk immediately."""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.apps_dir = config_dir / "apps"
        self.apps_dir.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for path in sorted(self.apps_dir.glob("*.json")):
            try:
                d = json.loads(path.read_text())
                name = d.get("name") or path.stem
                d["name"] = name
                out[name] = d
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def save(self, app_dict: dict) -> None:
        name = app_dict["name"]
        path = self.apps_dir / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(app_dict, indent=2))
        os.replace(tmp, path)

    def delete(self, name: str) -> bool:
        path = self.apps_dir / f"{name}.json"
        if path.exists():
            path.unlink()
            return True
        return False
