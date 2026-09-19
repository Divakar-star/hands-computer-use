"""Redaction: what may leave the process (model prompts, logs, artifacts, screenshots).

Three layers, because no single one is enough for regulated data:
  1. Pattern rules for well-formed PII (SSN, card numbers, emails, phones, API keys).
  2. *Learned* values: when the page shows `SSN: ...` / `Name: ...` (labels the
     policy marks sensitive) the value is remembered for the run and scrubbed
     everywhere it later appears - e.g. a member's name inside a page heading.
  3. Registered secrets / identifiers (the password, the member number) scrubbed
     verbatim, replaced by a placeholder that keeps logs readable ([member_id]).

Limits (also stated in REPORT.md): value-learning only helps once the labelled
field has been seen; free-text PII that no rule recognises will pass through.
This is a defence in depth on top of not sending sensitive fields to the model.
"""
from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"), "[API_KEY]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b"), "Bearer [TOKEN]"),
]


_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def _scrub_cards(s: str) -> str:
    """Only Luhn-valid 13-19 digit runs are card numbers; timestamps / ids are left alone."""
    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        plausible = 13 <= len(digits) <= 19 and digits[0] in "3456" and _luhn(digits)
        return "[CARD]" if plausible else m.group(0)
    return _CARD.sub(repl, s)


# Keys whose values are identifiers WE generate (run ids, paths, capability ids), never page data.
# Pattern-redacting them corrupts the evidence trail (a timestamp that passes a Luhn check, a
# 'cap@1.0.0' that looks like an email) without protecting anything.
_STRUCTURAL_KEYS = {"run_id", "ticket_id", "evidence", "screenshot", "dom_snapshot", "capability_or_goal",
                    "capability_id", "capability_version", "capability", "transcript_ref", "ts", "seq", "type",
                    "actor", "control_owner", "step", "step_id", "from_owner", "to_owner"}

_ID_DASH_NAME = re.compile(r"^\d{3,}\s*[-–]\s*(\S.*\S)$")


class Redactor:
    def __init__(self, sensitive_labels: list[str] | None = None):
        self._label_res = [re.compile(p, re.I) for p in (sensitive_labels or [])]
        self._learned: dict[str, str] = {}   # verbatim value -> placeholder
        self._model_ok: set[str] = set()     # values the model may see (it already has them from the goal)

    # -- registration -------------------------------------------------------------------
    def register(self, value: str | None, placeholder: str, model_visible: bool = False) -> None:
        """Scrub `value` (a secret or identifier) from anything persisted. `model_visible` is for
        values the caller itself gave the model (e.g. the member number in the goal): they stay
        readable in the prompt - hiding them would only confuse it - but never reach disk."""
        if value and len(value.strip()) >= 3:
            v = value.strip()
            self._learned[v] = placeholder
            if model_visible:
                self._model_ok.add(v)

    def is_sensitive_label(self, label: str) -> bool:
        return any(r.search(label) for r in self._label_res)

    def learn_fields(self, fields: list[tuple[str, str]]) -> None:
        """fields = (label, value) pairs read off a page; sensitive ones are remembered."""
        for label, value in fields:
            if self.is_sensitive_label(label):
                self.register(value, "[" + re.sub(r"\W+", "_", label.strip()).strip("_").upper()[:12] + "]")
            else:
                # 'id - Full Name' is the usual way legacy screens show a person next to a key
                m = _ID_DASH_NAME.match(value.strip())
                if m and re.search(r"[A-Za-z]{2}", m.group(1)):
                    self.register(m.group(1), "[NAME]")

    # -- application --------------------------------------------------------------------
    def text(self, s: str | None, model_view: bool = False) -> str:
        if not s:
            return s or ""
        # longest first so "900-12-3456" is replaced before any shorter overlapping value
        for val in sorted(self._learned, key=len, reverse=True):
            if model_view and val in self._model_ok:
                continue
            s = s.replace(val, self._learned[val])
        for rx, repl in _PATTERNS:
            s = rx.sub(repl, s)
        return _scrub_cards(s)

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: (v if k in _STRUCTURAL_KEYS else self.obj(v)) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    def changed(self, s: str) -> bool:
        return self.text(s) != s

    def screen_masks(self) -> list[str]:
        """Verbatim strings a screenshot must hide (used by the DOM masker)."""
        return sorted(self._learned, key=len, reverse=True)


def mask_value(s: str, keep: int = 2) -> str:
    """Partial mask for outputs persisted under sensitivity=financial: $4,721.37 -> $*,***.37"""
    if not s:
        return s
    tail = s[-keep:] if len(s) > keep else ""
    return re.sub(r"\d", "*", s[: len(s) - len(tail)]) + tail
