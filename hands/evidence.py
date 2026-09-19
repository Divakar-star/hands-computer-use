"""Run evidence: a structured, redacted event log + screenshots + DOM snapshots.

Everything written passes through the Redactor at the write boundary (not at
each call site), so forgetting to redact somewhere cannot leak. Screenshots are
masked *in the page* before capture by the surface; DOM snapshots are text-redacted.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .redact import Redactor


def new_run_id(kind: str) -> str:
    return f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"


class RunLog:
    def __init__(self, root: str | Path, run_id: str, redactor: Redactor,
                 owner: Callable[[], str] = lambda: "automation"):
        self.dir = Path(root) / run_id
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.redactor = redactor
        self._owner = owner
        self._seq = 0
        self._lock = threading.Lock()
        self._fh = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def set_owner_provider(self, fn: Callable[[], str]) -> None:
        self._owner = fn

    def event(self, type: str, msg: str = "", *, actor: str = "system", step: str | None = None,
              **data: Any) -> None:
        with self._lock:
            self._seq += 1
            self._write(type, msg, actor, step, data)

    def _write(self, type, msg, actor, step, data) -> None:
        rec = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "actor": actor,
            "control_owner": self._owner(),
            "type": type,
            "step": step,
            "msg": msg,
            "data": data,
        }
        self._fh.write(json.dumps(self.redactor.obj(rec), ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def shot(self, label: str, png: bytes) -> str:
        name = f"{self._seq:03d}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}.png"
        path = self.dir / "shots" / name
        path.write_bytes(png)
        return str(path.relative_to(self.dir.parent))

    def snapshot(self, label: str, text: str) -> str:
        name = f"{self._seq:03d}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}.txt"
        path = self.dir / name
        path.write_text(self.redactor.text(text), encoding="utf-8")
        return str(path.relative_to(self.dir.parent))

    def write_json(self, name: str, obj: Any) -> str:
        path = self.dir / name
        path.write_text(json.dumps(self.redactor.obj(obj), indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")
        return str(path.relative_to(self.dir.parent))

    def close(self) -> None:
        self._fh.close()
