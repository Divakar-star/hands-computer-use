"""Guardrail policy: the explicit, configurable allowlist and risk model.

Enforced in three independent places so a bug in one is not a breach:
  1. tool/step dispatch  - Policy.check_action() before anything is executed;
  2. the network layer   - Policy.url_allowed() backs a Playwright route guard that
                           aborts any request outside the allowlist;
  3. runtime re-derivation - risk is recomputed from the *resolved live control*
                           (its label, href, form action), so an artifact that
                           understates a step's risk cannot slip through.

Risk classes -> response (justified in REPORT.md):
  safe          run
  reversible    run (form entry commits nothing)
  irreversible  BLOCK unless the caller passed an explicit approval, else escalate to a human
  forbidden     never run, no override (sign-off, admin routes)
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from .schema import ActionType, Risk, max_risk


class RiskRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    risk: Risk
    name_regex: str | None = None         # accessible name / visible text of the control
    form_action_regex: str | None = None  # path the control's form posts to
    href_regex: str | None = None

    def matches(self, name: str, form_action: str | None, href: str | None) -> bool:
        if self.name_regex and re.search(self.name_regex, name or "", re.I):
            return True
        if self.form_action_regex and form_action and re.search(self.form_action_regex, form_action):
            return True
        if self.href_regex and href and re.search(self.href_regex, href):
            return True
        return False


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_origins: list[str]                                  # e.g. ["http://127.0.0.1:8765"]
    allowed_path_globs: list[str] = Field(default_factory=lambda: ["/msc/*"])
    denied_path_globs: list[str] = Field(default_factory=lambda: ["/__admin*"])
    allowed_actions: list[ActionType] = Field(default_factory=lambda: list(ActionType))
    risk_rules: list[RiskRule] = Field(default_factory=list)
    # Fallback for apps we have no explicit rules for: names that *sound* like a commit
    # are treated as irreversible. Fail-safe by design (false positives cost a human click).
    irreversible_name_heuristic: str = r"\b(confirm|submit|approve|delete|remove|close|post|send|transfer|pay|authorize)\b"
    sensitive_labels: list[str] = Field(default_factory=lambda: [
        r"\bssn\b", r"\btin\b", r"date of birth|\bdob\b", r"\bphone\b", r"\baddress\b", r"^name$",
        r"holder|owner|beneficiar"])
    forbidden_input_types: list[str] = Field(default_factory=lambda: ["password"])
    max_steps: int = 25
    step_timeout_ms: int = 8000

    # ------------------------------------------------------------------ allowlist
    def url_allowed(self, url: str) -> tuple[bool, str]:
        if url.startswith(("about:", "data:", "blob:")):
            return True, "inert scheme"
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        path = u.path or "/"
        if any(fnmatch.fnmatch(path, g) for g in self.denied_path_globs):
            return False, f"path {path} is explicitly denied"
        if path == "/" or any(fnmatch.fnmatch(path, g) for g in self.allowed_path_globs):
            return True, "ok"
        return False, f"path {path} not in allowed routes"

    # ------------------------------------------------------------------ risk
    def classify(self, action: ActionType, *, name: str = "", form_action: str | None = None,
                 href: str | None = None, input_type: str | None = None) -> tuple[Risk, str]:
        if action not in self.allowed_actions:
            return Risk.forbidden, f"action type {action.value} is not allowed by policy"
        if input_type in self.forbidden_input_types and action in (ActionType.fill, ActionType.select):
            return Risk.forbidden, f"agent may not enter data into {input_type} fields"
        base = {ActionType.fill: Risk.reversible, ActionType.select: Risk.reversible,
                ActionType.check: Risk.reversible}.get(action, Risk.safe)
        found = [Risk.safe]
        why = "default for action type"
        for rule in self.risk_rules:
            if rule.matches(name, form_action, href):
                found.append(rule.risk)
                why = f"rule '{rule.name}'"
        if action == ActionType.click and len(found) == 1 and re.search(
                self.irreversible_name_heuristic, name or "", re.I):
            found.append(Risk.irreversible)
            why = "name heuristic (looks like a commit)"
        risk = max_risk(base, *found)
        return risk, why

    def check_step_url(self, url: str) -> None:
        ok, why = self.url_allowed(url)
        if not ok:
            raise PolicyViolation(why)


class PolicyViolation(Exception):
    pass


def load_policy(path: str | Path) -> Policy:
    return Policy.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
