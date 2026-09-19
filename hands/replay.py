"""Deterministic replay: the production execution path. No model is called here.

Determinism comes from: resolving each control from semantic descriptors with an
ordered, cross-checked strategy cascade (matching.resolve); condition-based waits
(never fixed sleeps); a per-step postcondition that must hold before moving on;
and bounded, declared recoveries. Same inputs + same UI => same steps, same outputs.

What the engine does when a step's postcondition is not met is the interesting part.
It classifies the observed state, in this order, using lists carried *by the artifact*:

    recoverable      known interstitial / transient / expired session -> bounded, deliberate response
    business outcome an expected answer the caller needs ("no such member") -> return it, not an error
    failure signature a recognisable hard failure (raw app error) -> stop with evidence
    unknown          nothing matches -> escalate to a human (if enabled) or fail with evidence

Result statuses:  success | business_outcome | blocked | escalated | failed.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .control import ControlPlane, ControlViolation, InterventionRequest, new_ticket_id
from .evidence import RunLog
from .matching import (coerce, describe_condition, eval_condition, read_labeled_value, resolve, subst)
from .observation import Element, Observation
from .policy import Policy, PolicyViolation
from .redact import Redactor, mask_value
from .schema import (ActionType, Capability, Drift, Failure, FailureCategory, Handoff, Outcome, Recoverable,
                     Recovery, Result, Risk, Sensitivity, Step, ValueType, max_risk)
from .session import SessionProvider
from .surface import Surface


class _Done(Exception):
    def __init__(self, result: Result):
        self.result = result


class _Restart(Exception):
    pass


class _SkipAct(Exception):
    """Raised by the approval gate when a human performed the irreversible action themselves."""


@dataclass
class _Ctx:
    started: float
    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    reauths: int = 0
    irreversible_done: bool = False
    outputs: dict[str, Any] = field(default_factory=dict)
    recoveries: dict[tuple[str, str, str], Recovery] = field(default_factory=dict)
    drift: dict[tuple[str, str, str], Drift] = field(default_factory=dict)
    handoffs: list[Handoff] = field(default_factory=list)
    steps_completed: int = 0
    last_obs: Observation | None = None


def _transport_unhealthy(obs: Observation) -> bool:
    return any(f.status is not None and f.status >= 400 for f in obs.frames)


def validate_inputs(cap: Capability, inputs: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    out: dict[str, str] = {}
    known = {p.name for p in cap.inputs}
    for k in inputs:
        if k not in known:
            errors.append(f"unknown input '{k}'")
    for p in cap.inputs:
        raw = inputs.get(p.name)
        if raw is None or raw == "":
            if p.required:
                errors.append(f"missing required input '{p.name}'")
            continue
        s = ("true" if raw else "false") if isinstance(raw, bool) else str(raw).strip()
        if p.type == ValueType.integer and not re.fullmatch(r"-?\d+", s):
            errors.append(f"'{p.name}' must be an integer")
        elif p.type == ValueType.decimal:
            try:
                Decimal(s.replace("$", "").replace(",", ""))
            except InvalidOperation:
                errors.append(f"'{p.name}' must be a decimal number")
        elif p.type == ValueType.boolean and s.lower() not in ("true", "false"):
            errors.append(f"'{p.name}' must be true or false")
        if p.pattern and not re.fullmatch(p.pattern, s):
            errors.append(f"'{p.name}' does not match the required format")
        if p.enum and s not in p.enum:
            errors.append(f"'{p.name}' must be one of {p.enum}")
        out[p.name] = s
    return out, errors


class ReplayEngine:
    def __init__(self, surface: Surface, policy: Policy, redactor: Redactor, log: RunLog, *,
                 base_url: str, session: SessionProvider | None = None, control: ControlPlane | None = None,
                 tenant: str | None = None, approvals: set[str] | None = None,
                 on_stuck: str = "fail", shots: str = "failure", max_seconds: float = 120.0,
                 locate_grace_ms: int = 1500):
        self.s, self.policy, self.redactor, self.log = surface, policy, redactor, log
        self.base = base_url.rstrip("/")
        self.session, self.control, self.tenant = session, control, tenant
        self.approvals = approvals or set()
        self.on_stuck, self.shots, self.max_seconds = on_stuck, shots, max_seconds
        self.locate_grace_ms = locate_grace_ms

    # ================================================================== public entry
    def run(self, cap: Capability, inputs: dict[str, Any]) -> Result:
        t0 = time.monotonic()
        ctx = _Ctx(started=t0)
        res = Result(run_id=self.log.run_id, capability_id=cap.id, capability_version=cap.version,
                     tenant=self.tenant, status="failed")
        self.log.event("run_start", f"replay {cap.id}@{cap.version}", actor="replay", tenant=self.tenant,
                       capability_status=cap.status, max_risk=cap.review.max_risk.value)
        try:
            params, errors = validate_inputs(cap, inputs)
            if errors:
                raise _Done(self._fail(res, FailureCategory.invalid_input, None,
                                       "; ".join(errors), retriable=False))
            for p in cap.inputs:                      # keep identifiers / secrets out of every log
                if p.name in params and p.sensitivity != Sensitivity.none:
                    self.redactor.register(params[p.name], f"[{p.name}]")
            self._preflight(cap)
            while True:
                try:
                    self._establish(cap)
                    for i, step in enumerate(cap.steps):
                        self._run_step(cap, step, params, ctx)
                        ctx.steps_completed = i + 1
                    self._final_checkpoint(cap, params, ctx)
                    break
                except _Restart:
                    ctx.outputs.clear()
                    ctx.steps_completed = 0
                    continue
            res.status, res.outputs = "success", dict(ctx.outputs)
            res.message = "completed and verified the success checkpoint"
        except _Done as done:
            res = done.result
        except PolicyViolation as exc:
            res = self._fail(res, FailureCategory.policy_violation, None, str(exc), status="blocked")
        except ControlViolation as exc:
            res = self._fail(res, FailureCategory.internal_error, None, f"control fence: {exc}")
        except Exception as exc:  # last resort: still a structured result, with evidence
            ev = self._capture("internal-error")
            res = self._fail(res, FailureCategory.internal_error, None, f"{type(exc).__name__}: {exc}", evidence=ev)
        res.run_id, res.capability_id = self.log.run_id, cap.id
        res.capability_version, res.tenant = cap.version, self.tenant
        res.recoveries, res.drift = list(ctx.recoveries.values()), list(ctx.drift.values())
        res.handoffs, res.steps_completed = ctx.handoffs, ctx.steps_completed
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        if res.status in ("success", "business_outcome") and not res.outputs:
            res.outputs = dict(ctx.outputs)
        self._log_result(cap, res)
        return res

    # ================================================================== phases
    def _preflight(self, cap: Capability) -> None:
        for s in cap.steps:
            if s.action not in self.policy.allowed_actions:
                raise PolicyViolation(f"step {s.id}: action '{s.action.value}' not permitted by policy")
        self.policy.check_step_url(self.base + cap.entry)

    def _establish(self, cap: Capability) -> None:
        if self.session is not None:
            self.session.ensure(self.s)
        else:
            self.s.goto(self.base + cap.entry)
        self.log.event("session", "session established at entry", actor="replay", entry=cap.entry)

    def _run_step(self, cap: Capability, step: Step, params: dict[str, str], ctx: _Ctx) -> None:
        for attempt in (1, 2):                    # 2nd attempt only after a human said "retry_step"
            self._deadline(ctx)
            el, res = self._locate(cap, step, params, ctx)
            try:
                risk = self._gate(cap, step, el, ctx)
                self._act(step, el, params, res, risk)
            except _SkipAct:                      # a human performed the irreversible step themselves
                risk = Risk.irreversible
            if risk == Risk.irreversible:
                ctx.irreversible_done = True
            verdict = self._verify(cap, step, params, ctx)
            if verdict == "retry":
                continue
            if self.shots == "steps":
                self.log.shot(f"after-{step.id}", self.s.screenshot())
            return
        raise _Done(self._fail(self._blank(cap), FailureCategory.handoff_failed, step.id,
                               "step still failing after a human retry"))

    # ------------------------------------------------------------------ locate
    def _locate(self, cap, step, params, ctx):
        if step.target is None:
            return None, None
        deadline = time.monotonic() + min(step.timeout_ms, self.locate_grace_ms) / 1000
        while True:
            obs = self.s.observe()
            ctx.last_obs = obs
            r = resolve(step.target, obs, params)
            if r.status == "ok":
                for kind, detail in r.drift:
                    ctx.drift.setdefault((step.id, kind, detail), Drift(step_id=step.id, kind=kind, detail=detail))
                return r.element, r
            if self._respond_to_state(cap, step, obs, params, ctx):
                continue
            if time.monotonic() > deadline:
                if r.status == "none" and step.on_target_missing:
                    out = next((o for o in cap.outcomes if o.code == step.on_target_missing), None)
                    if out is not None:
                        raise _Done(self._outcome(cap, out, obs))
                cat = FailureCategory.target_ambiguous if r.status == "ambiguous" else FailureCategory.target_not_found
                ev = self._capture(f"{step.id}-target")
                raise _Done(self._fail(self._blank(cap), cat, step.id,
                                       f"could not resolve control: {step.target.description}",
                                       expected=f"exactly one match; strategies tried {r.tried}",
                                       observed=self._observed(obs), evidence=ev))
            self.s.wait(100)

    # ------------------------------------------------------------------ gate (risk)
    def _gate(self, cap, step, el: Element | None, ctx) -> Risk:
        computed, why = self.policy.classify(
            step.action, name=(el.name or el.text) if el else "", form_action=el.form_action if el else None,
            href=el.href if el else None, input_type=el.type if el else None)
        risk = max_risk(step.risk, computed)
        if risk != step.risk:
            self.log.event("risk_escalated", f"runtime risk {computed.value} ({why}) exceeds declared {step.risk.value}",
                           actor="replay", step=step.id)
        if risk == Risk.forbidden:
            raise _Done(self._fail(self._blank(cap), FailureCategory.policy_violation, step.id,
                                   f"forbidden action: {why}", status="blocked"))
        if risk == Risk.irreversible and step.id not in self.approvals and "*" not in self.approvals:
            desc = step.target.description if step.target else step.action.value
            summary = (f"Step '{step.intent}' is IRREVERSIBLE ({why}: {desc}). "
                       f"Approve = 'Retry the step' (automation performs it); 'I completed the step' if you did it; Abort = deny.")
            ticket = self._escalate(cap, step, "needs_approval", summary, ctx)
            if ticket is None or ticket.resume in (None, "abort"):
                raise _Done(self._fail(self._blank(cap), FailureCategory.policy_violation, step.id,
                                       "irreversible step requires approval and none was given",
                                       expected="approval for this step (approvals={...})", status="blocked",
                                       retriable=True))
            if ticket.resume == "next_step":       # the human performed it themselves
                raise _SkipAct()
            self.log.event("approval", "human approved the irreversible step", actor="human", step=step.id)
        return risk

    # ------------------------------------------------------------------ act
    def _act(self, step, el, params, res, risk) -> None:
        val = self._value(step, params)
        self.log.event("step", step.intent, actor="replay", step=step.id, action=step.action.value,
                       target=step.target.description if step.target else None,
                       locator_used=res.used.kind if res and res.used else None,
                       locators_tried=res.tried if res else None, risk=risk.value,
                       value=self._loggable(step, val))
        if step.action == ActionType.navigate:
            self.s.goto(self.base + subst(str(val), params))
        elif step.action == ActionType.wait:
            self.s.wait(int(val or 500))
        elif step.action != ActionType.extract:
            self.s.perform(step.action, el, val)

    def _value(self, step: Step, params: dict[str, str]):
        if step.value is None:
            return None
        if step.value.param is not None:
            v = params[step.value.param]
            return v.lower() == "true" if step.action == ActionType.check else v
        return step.value.literal

    def _loggable(self, step: Step, val):
        return None if val is None else self.redactor.text(str(val))

    # ------------------------------------------------------------------ verify (+ classify/respond)
    def _verify(self, cap, step, params, ctx) -> str:
        def ready(obs: Observation) -> bool:
            if _transport_unhealthy(obs):       # an error page at the expected URL is not the expected state
                return False
            if step.expect is not None and not eval_condition(step.expect, obs, params):
                return False
            return all(read_labeled_value(obs, e.labels, e.frame) is not None for e in step.extractions)

        deadline = time.monotonic() + step.timeout_ms / 1000
        handoff_used = False
        while True:
            self._deadline(ctx)
            obs = self.s.observe()
            ctx.last_obs = obs
            if ready(obs):
                self._extract(cap, step, obs, ctx)
                return "ok"
            if self._respond_to_state(cap, step, obs, params, ctx):
                continue
            if time.monotonic() <= deadline:
                self.s.wait(120)
                continue
            # nothing we recognise explains why the checkpoint is not met
            expected = describe_condition(step.expect, params) if step.expect else \
                "labels " + ", ".join(l for e in step.extractions for l in e.labels)
            if handoff_used:
                raise _Done(self._fail(self._blank(cap), FailureCategory.handoff_failed, step.id,
                                       "checkpoint still not met after human hand-back",
                                       expected=expected, observed=self._observed(obs)))
            summary = f"Step '{step.intent}': expected {expected} but the screen shows something unfamiliar."
            ticket = self._escalate(cap, step, "unexpected_state", summary, ctx, observed=self._observed(obs))
            if ticket is None:
                ev = self._capture(f"{step.id}-checkpoint")
                cat = FailureCategory.checkpoint_failed
                raise _Done(self._fail(self._blank(cap), cat, step.id,
                                       f"checkpoint not met after step: {step.intent}",
                                       expected=expected, observed=self._observed(obs), evidence=ev))
            if ticket.resume == "abort" or ticket.resume is None:
                raise _Done(self._escalated(cap, step, ticket))
            handoff_used = True
            if ticket.resume == "retry_step":
                return "retry"
            deadline = time.monotonic() + 2.0       # next_step: trust, but verify the checkpoint

    def _extract(self, cap, step, obs, ctx) -> None:
        specs = {o.name: o for o in cap.outputs}
        for e in step.extractions:
            raw = read_labeled_value(obs, e.labels, e.frame)
            spec = specs[e.output]
            try:
                ctx.outputs[e.output] = coerce(raw, spec.type)
            except ValueError as exc:
                raise _Done(self._fail(self._blank(cap), FailureCategory.checkpoint_failed, step.id,
                                       f"output '{e.output}' unreadable: {exc}", observed=raw))
            self.log.event("extract", f"read '{e.output}'", actor="replay", step=step.id, output=e.output,
                           value=self._log_output(spec, ctx.outputs[e.output]))

    def _final_checkpoint(self, cap, params, ctx) -> None:
        deadline = time.monotonic() + 3.0
        while True:
            obs = self.s.observe()
            if not _transport_unhealthy(obs) and eval_condition(cap.success, obs, params):
                self.log.event("checkpoint", "success checkpoint verified", actor="replay",
                               condition=describe_condition(cap.success, params))
                return
            if self._respond_to_state(cap, cap.steps[-1], obs, params, ctx):
                continue
            if time.monotonic() > deadline:
                ev = self._capture("final-checkpoint")
                raise _Done(self._fail(self._blank(cap), FailureCategory.checkpoint_failed, cap.steps[-1].id,
                                       "success checkpoint not met",
                                       expected=describe_condition(cap.success, params),
                                       observed=self._observed(obs), evidence=ev))
            self.s.wait(120)

    # ------------------------------------------------------------------ state classification
    def _respond_to_state(self, cap, step, obs, params, ctx) -> bool:
        """Returns True if a recoverable was handled (caller re-observes). Raises _Done for
        business outcomes and hard failures. Returns False if nothing recognised."""
        for rec in cap.recoverables:
            if eval_condition(rec.when, obs, params):
                self._recover(cap, step, rec, obs, params, ctx)
                return True
        for out in cap.outcomes:
            if out.when is not None and eval_condition(out.when, obs, params):
                raise _Done(self._outcome(cap, out, obs))
        for sig in cap.failure_signatures:
            if eval_condition(sig.when, obs, params):
                ev = self._capture(f"{step.id}-{sig.code}")
                cat = FailureCategory.app_error
                raise _Done(self._fail(self._blank(cap), cat, step.id, f"{sig.code}: {sig.description}",
                                       expected=f"step '{step.intent}' to complete",
                                       observed=self._observed(obs), retriable=sig.retriable, evidence=ev))
        if obs.dialog:
            ev = self._capture(f"{step.id}-dialog")
            raise _Done(self._fail(self._blank(cap), FailureCategory.unexpected_state, step.id,
                                   f"unexpected browser dialog was cancelled: {obs.dialog}", evidence=ev))
        return False

    def _recover(self, cap, step, rec: Recoverable, obs, params, ctx) -> None:
        key = (step.id, rec.code)
        ctx.counts[key] = ctx.counts.get(key, 0) + 1
        n = ctx.counts[key]
        h = rec.handler
        if n > rec.max_times:
            cat = {"reauth": FailureCategory.session_expired}.get(h.kind, FailureCategory.app_error
                   if h.kind in ("reload_frame", "wait_retry") else FailureCategory.unexpected_state)
            ev = self._capture(f"{step.id}-{rec.code}-exhausted")
            raise _Done(self._fail(self._blank(cap), cat, step.id,
                                   f"recoverable '{rec.code}' persisted after {rec.max_times} attempt(s)",
                                   expected=f"step '{step.intent}' to complete", observed=self._observed(obs),
                                   retriable=h.kind != "click", evidence=ev))
        self.log.event("recovering", f"{rec.code}: {rec.description} (attempt {n}/{rec.max_times})",
                       actor="replay", step=step.id, handler=h.kind)
        if h.kind == "click":
            assert h.target is not None
            r = resolve(h.target, obs, params)
            if r.status != "ok":
                raise _Done(self._fail(self._blank(cap), FailureCategory.unexpected_state, step.id,
                                       f"recoverable '{rec.code}' matched but its handler control was not found"))
            risk, why = self.policy.classify(ActionType.click, name=r.element.name or r.element.text,
                                             form_action=r.element.form_action, href=r.element.href)
            if risk in (Risk.irreversible, Risk.forbidden):
                raise _Done(self._fail(self._blank(cap), FailureCategory.policy_violation, step.id,
                                       f"recovery '{rec.code}' would perform a {risk.value} action", status="blocked"))
            self.s.perform(ActionType.click, r.element)
        elif h.kind in ("reload_frame", "wait_retry"):
            self.s.wait(h.delay_ms * n)            # linear backoff, bounded by max_times
            frame = h.frame or next((f.name for f in obs.frames if f.status and f.status >= 500), "body")
            self.s.reload_frame(frame)
        elif h.kind == "reauth":
            if ctx.irreversible_done or ctx.reauths >= 1 or self.session is None:
                raise _Done(self._fail(self._blank(cap), FailureCategory.session_expired, step.id,
                                       "session expired and it is not safe/possible to restart automatically",
                                       retriable=not ctx.irreversible_done))
            ctx.reauths += 1
            self._note_recovery(ctx, rec, step, "re-authenticated and restarted the flow from step 1")
            raise _Restart()
        self._note_recovery(ctx, rec, step, {"click": "clicked the declared handler control",
                                             "reload_frame": "backed off and reloaded the frame",
                                             "wait_retry": "waited and retried"}.get(h.kind, h.kind))

    def _note_recovery(self, ctx, rec, step, action) -> None:
        k = (rec.code, step.id, action)
        if k in ctx.recoveries:
            ctx.recoveries[k].attempts += 1
        else:
            ctx.recoveries[k] = Recovery(code=rec.code, step_id=step.id, action=action)

    # ------------------------------------------------------------------ escalation
    def _escalate(self, cap, step, reason, summary, ctx, observed: str | None = None):
        """Returns a closed Ticket, or None when escalation is not enabled (caller fails)."""
        if self.control is None or (self.on_stuck != "escalate" and reason != "needs_approval"):
            return None
        if reason == "needs_approval" and self.control.operator is None:
            return None
        ev = self._capture(f"{step.id}-{reason}")
        obs = ctx.last_obs
        req = InterventionRequest(
            ticket_id=new_ticket_id(), reason=reason, summary=self.redactor.text(summary), run_id=self.log.run_id,
            capability_or_goal=f"{cap.id}@{cap.version}", step_id=step.id,
            url=self.redactor.text(obs.frames[-1].url if obs and obs.frames else ""),
            context={"step_intent": step.intent, "observed": observed or (self._observed(obs) if obs else ""),
                     "inputs_provided": [p.name for p in cap.inputs]},
            evidence=ev)
        ticket = self.control.escalate(self.s, req)
        ctx.handoffs.append(Handoff(ticket_id=req.ticket_id, reason=reason, step_id=step.id,
                                    resolution={"expired": "timed_out"}.get(ticket.state, ticket.resume or ticket.state),
                                    human_actions=len(ticket.human_actions)))
        if ticket.state in ("expired",) or ticket.resume is None:
            ticket.resume = "abort"
        return ticket

    def _escalated(self, cap, step, ticket) -> Result:
        r = self._blank(cap)
        r.status = "escalated"
        r.failure = Failure(category=FailureCategory.unexpected_state, step_id=step.id,
                            message=f"run stopped for human intervention; ticket {ticket.request.ticket_id} "
                                    f"ended '{ticket.state}'", retriable=True)
        r.message = ticket.note or "human abort / no response"
        return r

    # ------------------------------------------------------------------ results & evidence
    def _blank(self, cap) -> Result:
        return Result(run_id=self.log.run_id, capability_id=cap.id, capability_version=cap.version,
                      tenant=self.tenant, status="failed")

    def _fail(self, res: Result, cat: FailureCategory, step_id: str | None, message: str, *,
              expected: str | None = None, observed: str | None = None, retriable: bool = False,
              evidence: dict[str, str] | None = None, status: str = "failed") -> Result:
        res.status = status  # type: ignore[assignment]
        res.failure = Failure(category=cat, step_id=step_id, message=self.redactor.text(message),
                              expected=expected and self.redactor.text(expected),
                              observed=observed and self.redactor.text(observed),
                              retriable=retriable, evidence=evidence or {})
        return res

    def _outcome(self, cap, out: Outcome, obs: Observation) -> Result:
        r = self._blank(cap)
        r.status, r.outcome = "business_outcome", out.code
        r.message = out.caller_guidance or out.description
        self.log.event("business_outcome", out.description, actor="replay", outcome=out.code,
                       observed=self._observed(obs))
        return r

    def _observed(self, obs: Observation | None, limit: int = 400) -> str:
        if obs is None:
            return ""
        parts = [f"[{f.name}] {f.url} status={f.status}: " + re.sub(r"\s+", " ", f.text)[:limit]
                 for f in obs.frames if f.text.strip()]
        return self.redactor.text(" | ".join(parts))[: limit * 2]

    def _capture(self, label: str) -> dict[str, str]:
        ev: dict[str, str] = {}
        try:
            ev["screenshot"] = self.log.shot(label, self.s.screenshot())
            ev["dom_snapshot"] = self.log.snapshot(label, self.s.snapshot_text())
        except Exception as exc:                    # evidence must never mask the real failure
            self.log.event("evidence_error", str(exc))
        return ev

    def _log_output(self, spec, value):
        if spec.sensitivity in (Sensitivity.pii, Sensitivity.secret):
            return "[REDACTED]"
        if spec.sensitivity == Sensitivity.financial:
            return mask_value(str(value))
        return value

    def _log_result(self, cap, res: Result) -> None:
        safe = res.model_dump(mode="json")
        specs = {o.name: o for o in cap.outputs}
        safe["outputs"] = {k: self._log_output(specs[k], v) if k in specs else "[?]" for k, v in res.outputs.items()}
        self.log.event("run_end", res.status + (f" ({res.outcome})" if res.outcome else ""), actor="replay",
                       result=safe)
        self.log.write_json("result.json", safe)

    def _deadline(self, ctx: _Ctx) -> None:
        if time.monotonic() - ctx.started > self.max_seconds:
            raise _Done(self._fail(self._blank_unknown(), FailureCategory.timeout, None,
                                   f"run exceeded {self.max_seconds}s", retriable=True))

    def _blank_unknown(self) -> Result:
        return Result(run_id=self.log.run_id, capability_id="?", capability_version="?", status="failed")

