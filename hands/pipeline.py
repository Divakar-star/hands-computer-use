"""Orchestration: discover -> record -> verify, and replay.

Verification is what turns a recording into something we trust: the compiled artifact
is replayed in a FRESH browser session with the discovery inputs and must reproduce
the values the model read. Only then is it marked `verified` (a human still moves it
to `approved`).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent import DiscoveryAgent, DiscoveryResult
from .control import ControlPlane, Operator
from .evidence import RunLog, new_run_id
from .llm import ModelClient, estimate_cost
from .matching import coerce
from .policy import Policy
from .recorder import Recorder, RecordingError, load_profile, save_capability
from .redact import Redactor
from .replay import ReplayEngine
from .schema import Capability, Result, ValueType, Verification
from .session import MscSession
from .surface import PlaywrightSurface
from .tenancy import TenantOverride, apply_override


def origin_of(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url


def policy_for(policy: Policy, target: str) -> Policy:
    return policy.model_copy(update={"allowed_origins": [origin_of(target)]})


def discover(*, target: str, goal: str, model: ModelClient, policy: Policy, profile_path: str | Path,
             out_dir: str | Path, cap_dir: str | Path, headless: bool = True, tenant: str | None = None,
             cap_id: str | None = None, masks: dict[str, str] | None = None, max_steps: int | None = None,
             token_budget: int | None = None, vision: bool = False, operator: Operator | None = None, on_stuck: str = "escalate",
             verify: bool = True, product_version: str | None = None,
             say: Callable[[str], None] = print) -> tuple[Capability | None, DiscoveryResult, Result | None]:
    policy = policy_for(policy, target)
    profile = load_profile(profile_path)
    redactor = Redactor(policy.sensitive_labels)
    for placeholder, value in (masks or {}).items():
        redactor.register(value, f"[{placeholder}]", model_visible=True)
    run_id = new_run_id("discovery")
    control = ControlPlane(operator=operator)
    log = RunLog(out_dir, run_id, redactor, owner=lambda: control.owner)
    control.log = log
    say(f"discovery run {run_id} using model {model.name}")
    cap = verification = None
    with PlaywrightSurface(policy, redactor, headless=headless, guard=control.assert_automation) as surface:
        MscSession(target, redactor).ensure(surface)
        agent = DiscoveryAgent(surface, model, policy, redactor, log, base_url=target, control=control,
                               max_steps=max_steps, vision=vision, on_stuck=on_stuck,
                               token_budget=token_budget)
        result = agent.run(goal, profile.get("entry", "/"))
        shot_end = log.shot("final-screen", surface.screenshot())
        say(f"agent stopped: {result.stop_reason} after {result.steps_used} step(s) - {result.summary}")
        say(f"tokens: {result.usage}  {estimate_cost(model.name, result.usage)}")
        if result.success:
            try:
                recorder = Recorder(policy, redactor, profile)
                cap = recorder.compile(
                    result, goal=goal, model=model.name, run_id=run_id, tenant=tenant,
                    product_version=product_version, transcript_ref=f"{run_id}/events.jsonl", cap_id=cap_id)
                result.examples = dict(recorder.last_examples)
            except RecordingError as exc:
                log.event("recording_failed", str(exc), actor="system")
                say(f"recording failed: {exc}")
        log.event("artifact", "compiled" if cap else "not compiled", actor="system", screenshot=shot_end,
                  capability=cap.id if cap else None)
    if cap is not None:
        path = save_capability(cap, Path(cap_dir) / f"{cap.id}.json")
        say(f"artifact written: {path} (status={cap.status})")
        if verify:
            verification = _verify(cap, result, recorder.last_examples, target, policy, out_dir, headless, say)
            cap.verification = Verification(passed=verification[0], run_id=verification[1].run_id,
                                            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                            note=verification[2])
            cap.status = "verified" if verification[0] else "draft"
            save_capability(cap, path)
            say(f"verification replay: {'PASSED' if verification[0] else 'FAILED'} - {verification[2]}")
    log.close()
    return cap, result, verification[1] if verification else None


def _verify(cap: Capability, d: DiscoveryResult, examples: dict[str, str], target: str, policy: Policy, out_dir,
            headless: bool, say) -> tuple[bool, Result, str]:
    inputs = {p.name: examples[p.name] for p in cap.inputs if p.name in examples}
    expected = {t.extraction["name"]: t.extraction["value"] for t in d.trace if t.extraction and t.ok}
    res = replay(cap, inputs, target=target, policy=policy, out_dir=out_dir, headless=headless, label="verify")
    if res.status != "success":
        return False, res, f"replay ended {res.status}: {res.failure.message if res.failure else res.message}"
    specs = {o.name: o for o in cap.outputs}
    for name, raw in expected.items():
        want = coerce(raw, specs[name].type)
        if res.outputs.get(name) != want:
            return False, res, f"output '{name}' differs from what the model read"
    return True, res, f"reproduced {len(expected)} model-read value(s) in a fresh session with no LLM"


def replay(cap: Capability, inputs: dict[str, Any], *, target: str, policy: Policy, out_dir: str | Path,
           headless: bool = True, tenant: str | None = None, override: TenantOverride | None = None,
           approvals: set[str] | None = None, operator: Operator | None = None, on_stuck: str = "fail",
           shots: str = "failure", label: str = "replay", masks: dict[str, str] | None = None,
           surface_hook: Callable[[PlaywrightSurface, ControlPlane], None] | None = None,
           before_run: Callable[[PlaywrightSurface], None] | None = None) -> Result:
    policy = policy_for(policy, target)
    if override is not None:
        cap = apply_override(cap, override)
        tenant = tenant or override.tenant
    redactor = Redactor(policy.sensitive_labels)
    for placeholder, value in (masks or {}).items():
        redactor.register(value, f"[{placeholder}]")
    run_id = new_run_id(label)
    control = ControlPlane(operator=operator)
    log = RunLog(out_dir, run_id, redactor, owner=lambda: control.owner)
    control.log = log
    try:
        with PlaywrightSurface(policy, redactor, headless=headless, guard=control.assert_automation) as surface:
            if surface_hook:
                surface_hook(surface, control)
            engine = ReplayEngine(surface, policy, redactor, log, base_url=target,
                                  session=MscSession(target, redactor), control=control, tenant=tenant,
                                  approvals=approvals, on_stuck=on_stuck, shots=shots)
            if before_run:
                before_run(surface)
            return engine.run(cap, inputs)
    finally:
        log.close()


_ = time
