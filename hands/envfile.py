"""Tiny .env loader so API keys live in a git-ignored file, never on the command line or in chat.
Existing environment variables win; comments and blank lines are ignored."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: str | Path | None = None) -> list[str]:
    """Returns the NAMES of variables it set (never values)."""
    p = Path(path) if path else ROOT / ".env"
    if not p.is_file():
        return []
    set_names: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip().removeprefix("export ").strip(), val.strip().strip("'\"")
        if key and val and key not in os.environ:
            os.environ[key] = val
            set_names.append(key)
    return set_names
