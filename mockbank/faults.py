"""Runtime fault injection for the mock console.

These switches let the test harness (never the agent) put the app into the
exceptional states a replay has to cope with. They are toggled over the
localhost-only /__admin/ endpoints, or preset with MSC_FAULTS / --faults.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass
class Faults:
    # Compliance interstitial shown once per session, in the middle of a lookup.
    notice: bool = False
    # Artificial latency (ms) on member / account pages.
    slow_ms: int = 0
    # The next N member-detail loads answer 503 "temporarily unavailable" (transient).
    flaky_member: int = 0
    # Account pages answer 500 with a raw database error (hard app failure).
    app_error_acct: bool = False
    # A session dies once, after this many page loads (session-timeout simulation). 0 = off.
    expire_after: int = 0

    def update(self, values: dict) -> None:
        known = {f.name: f.type for f in fields(self)}
        for key, val in values.items():
            if key not in known:
                raise KeyError(f"unknown fault: {key}")
            current = getattr(self, key)
            setattr(self, key, _coerce(current, val))

    def as_dict(self) -> dict:
        return asdict(self)


def _coerce(current, val):
    if isinstance(current, bool):
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    return int(val)


def parse_spec(spec: str) -> Faults:
    """'notice,slow_ms=2500,flaky_member=1' -> Faults. Bare names mean True."""
    f = Faults()
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        key, sep, val = part.partition("=")
        f.update({key: val if sep else True})
    return f
