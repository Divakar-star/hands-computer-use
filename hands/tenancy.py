"""Per-tenant specialisation of a shared capability.

Many institutions run the same vendor product with different wording and
configuration. Re-recording per tenant would be O(tenants x capabilities). Instead
one *base* artifact is recorded once, and a tenant supplies a small, reviewable
OVERRIDE that only ADDS things - extra fallback locators and label aliases. An
override can never remove a locator, change a step's action/risk or widen policy,
so specialising cannot make a capability less safe than its base.

Drift management: the override pins the base artifact's digest. If the base is
re-recorded/edited the override is reported stale (and refused unless forced), so
nobody silently runs a tenant patch against a flow it was not written for.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .recorder import canonical_digest
from .schema import Capability, Locator


class StepPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_locators: list[Locator] = Field(default_factory=list)          # appended as fallbacks
    add_extraction_labels: dict[str, list[str]] = Field(default_factory=dict)   # output -> aliases


class TenantOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    capability_id: str
    base_version: str
    base_digest: str | None = None
    notes: str = ""
    step_patches: dict[str, StepPatch] = Field(default_factory=dict)


class StaleOverride(Exception):
    pass


def load_override(path: str | Path) -> TenantOverride:
    return TenantOverride.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def apply_override(cap: Capability, ov: TenantOverride, force: bool = False) -> Capability:
    if ov.capability_id != cap.id:
        raise StaleOverride(f"override is for {ov.capability_id}, not {cap.id}")
    if not force and (ov.base_version != cap.version or (ov.base_digest and ov.base_digest != canonical_digest(cap))):
        raise StaleOverride(f"override for tenant '{ov.tenant}' was written against {ov.capability_id}@{ov.base_version} "
                            f"({ov.base_digest}); base is now {cap.version} ({cap.digest}). Re-review the override.")
    out = cap.model_copy(deep=True)
    steps = {s.id: s for s in out.steps}
    for sid, patch in ov.step_patches.items():
        if sid not in steps:
            raise StaleOverride(f"override patches unknown step {sid}")
        step = steps[sid]
        if patch.add_locators:
            if step.target is None:
                raise StaleOverride(f"step {sid} has no target to add locators to")
            step.target.locators = [*step.target.locators, *patch.add_locators]
        for output, labels in patch.add_extraction_labels.items():
            for e in step.extractions:
                if e.output == output:
                    e.labels = [*e.labels, *[l for l in labels if l not in e.labels]]
    return out
