"""Goal-driven discovery agent: observe -> decide -> act, with the LLM in the loop.

The model is stateless per step: each turn it gets the goal, a compact history of
its own actions and their outcomes, and a redacted text rendering of the *current*
screen (frames, visible text, label/value pairs, numbered controls). It must answer
with exactly one tool call carrying a rationale. Refs are only valid for the screen
they were listed on, so the loop re-observes after every action.

Everything the model does is filtered by the same Policy replay uses, and every
action becomes a TraceStep holding the *descriptor* of the control (not a selector),
which is what the recorder turns into a capability.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .control import ControlPlane, InterventionRequest, new_ticket_id
from .evidence import RunLog
from .llm import ModelClient, ToolCall, ToolSpec
from .matching import coerce, read_labeled_value
from .observation import Element, Observation
from .policy import Policy, PolicyViolation
from .redact import Redactor, mask_value
from .schema import ActionType, Risk, ValueType
from .surface import Surface

SYSTEM = """You operate a legacy back-office banking web application through tools. There is no API: you work \
the UI like a human operator. Each turn you see a text rendering of the current screen (frames, visible text, \
label/value fields, and a numbered list of interactive controls) and must reply with EXACTLY ONE tool call.

Rules:
- Control refs like [7] are valid only for the screen shown this turn.
- Sign-on is already done. Never enter credentials or use sign-off / logout.
- Some values appear masked (e.g. [NAME], [SSN_TIN]). That is intentional data protection. Do not try to work \
around it, and never extract masked/sensitive fields.
- NEVER perform irreversible actions (submit, confirm, post, approve, delete, close). If the goal can only be \
completed by one, stop and call finish with success=false. If the goal is only to REACH a confirmation/review \
screen, stop there.
- Use extract_value to capture each piece of data the goal asks for. `label` must be the exact visible label text \
next to the value. Extract from the screen where the value is actually shown.
- A form may require ticking a consent / disclosure / certification checkbox. Tick it to proceed, but declare it \
in finish as a boolean parameter (example_value 'true'): only the caller may attest consent, never the capability.
- Prefer the shortest reliable path using clicks on links/buttons. Use goto only if no control leads there.
- If an error/validation message appears, read it and correct course. If you are stuck, call ask_human.
- When the goal is achieved call finish(success=true) with: capability.slug/description (written for another AI \
agent deciding whether to call this capability); parameters = the business inputs you used, generalised \
(snake_case name, description, type, example_value = the exact string you typed, sensitivity; member/account \
numbers are 'identifier'; add `pattern`, a regex, whenever the input has an evident fixed format such as digits \
only); needed_steps = the history step numbers REQUIRED to reproduce the result (exclude \
dead ends, corrections and failed attempts; include your extract_value steps); conditional_steps = for EVERY step \
where you pick one item out of a list or menu that depends on the input (e.g. clicking a particular account or \
record row), ask 'could this item legitimately not exist for some inputs?' and if yes declare it with an \
outcome_code (e.g. no_savings_account) and a description; \
success_evidence = 1-3 static texts (headings/labels, not data values) visible on the final screen that prove \
the goal state."""

_R = {"rationale": {"type": "string", "description": "one sentence: why this action"}}


def _tool(name, desc, props, required):
    return ToolSpec(name, desc, {"type": "object", "properties": {**props, **_R},
                                 "required": [*required, "rationale"], "additionalProperties": False})


TOOLS = [
    _tool("click", "Click a link/button/control.", {"ref": {"type": "integer"}}, ["ref"]),
    _tool("type_text", "Replace the contents of a text box.", {"ref": {"type": "integer"}, "text": {"type": "string"}},
          ["ref", "text"]),
    _tool("select_option", "Choose an option in a dropdown by its visible text or value.",
          {"ref": {"type": "integer"}, "option": {"type": "string"}}, ["ref", "option"]),
    _tool("set_checked", "Tick or untick a checkbox / choose a radio button.",
          {"ref": {"type": "integer"}, "checked": {"type": "boolean"}}, ["ref", "checked"]),
    _tool("press_key", "Press a keyboard key (e.g. Enter).", {"key": {"type": "string"}}, ["key"]),
    _tool("goto", "Navigate to an application path (e.g. /msc/inq). Last resort.", {"path": {"type": "string"}},
          ["path"]),
    _tool("extract_value", "Capture a value shown on screen as a named output. Verified against the page.",
          {"name": {"type": "string", "description": "snake_case output name"},
           "label": {"type": "string", "description": "exact visible label next to the value"},
           "type": {"type": "string", "enum": ["string", "integer", "decimal", "boolean", "date"]},
           "description": {"type": "string"}}, ["name", "label", "type", "description"]),
    _tool("ask_human", "Ask a human operator to take over the live session.", {"reason": {"type": "string"}},
          ["reason"]),
    _tool("finish", "End the run. success=true only if the goal state is reached.",
          {"success": {"type": "boolean"}, "summary": {"type": "string"},
           "capability": {"type": "object", "properties": {"slug": {"type": "string"},
                                                            "description": {"type": "string"}}},
           "parameters": {"type": "array", "items": {"type": "object", "properties": {
               "name": {"type": "string"}, "description": {"type": "string"},
               "type": {"type": "string", "enum": ["string", "integer", "decimal", "boolean"]},
               "example_value": {"type": "string"}, "pattern": {"type": "string"},
               "sensitivity": {"type": "string", "enum": ["none", "identifier", "pii"]}},
               "required": ["name", "description", "type", "example_value"]}},
           "needed_steps": {"type": "array", "items": {"type": "integer"}},
           "conditional_steps": {"type": "array", "items": {"type": "object", "properties": {
               "step": {"type": "integer"}, "outcome_code": {"type": "string"},
               "description": {"type": "string"}}, "required": ["step", "outcome_code", "description"]}},
           "success_evidence": {"type": "array", "items": {"type": "string"}}},
          ["success", "summary"]),
]


@dataclass
class TraceStep:
    n: int
    action: ActionType
    element: Element | None
    snapshot: list[Element]               # everything on screen at decision time (uniqueness proof)
    value: str | None
    frames_before: dict[str, str]
    frames_after: dict[str, str]
    risk: Risk
    rationale: str
    ok: bool = True
    extraction: dict[str, Any] | None = None
    human: bool = False


@dataclass
class DiscoveryResult:
    success: bool
    stop_reason: str
    summary: str
    trace: list[TraceStep]
    finish: dict[str, Any]
    final_obs: Observation | None
    steps_used: int
    usage: dict[str, int] = field(default_factory=dict)
    human_assisted: bool = False
    examples: dict[str, str] = field(default_factory=dict)   # normalised example inputs (set by the recorder)


def render(obs: Observation, redactor: Redactor, text_limit: int = 700) -> str:
    lines = ["FRAMES:"]
    for f in obs.frames:
        path = re.sub(r"^https?://[^/]+", "", f.url)
        lines.append(f"- {f.name}: {path}" + (f" (HTTP {f.status})" if f.status else ""))
    for f in obs.frames:
        t = re.sub(r"[ \t]+", " ", f.text).strip()
        if t:
            lines.append(f"PAGE TEXT [{f.name}]:\n{t[:text_limit]}")
    fields = obs.all_fields()
    if fields:
        lines.append("LABEL/VALUE FIELDS:")
        lines += [f"  {l}: {v}" for l, v in fields]
    lines.append("CONTROLS:")
    lines += [e.brief() for e in obs.elements]
    return redactor.text("\n".join(lines), model_view=True)


class DiscoveryAgent:
    def __init__(self, surface: Surface, model: ModelClient, policy: Policy, redactor: Redactor, log: RunLog, *,
                 base_url: str, control: ControlPlane | None = None, max_steps: int | None = None,
                 vision: bool = False, on_stuck: str = "escalate", token_budget: int | None = None):
        self.s, self.model, self.policy, self.redactor, self.log = surface, model, policy, redactor, log
        self.base = base_url.rstrip("/")
        self.control, self.vision, self.on_stuck = control, vision, on_stuck
        self.max_steps = max_steps or policy.max_steps
        self.token_budget = token_budget       # hard cap on input+output tokens for the whole run

    # ------------------------------------------------------------------ main loop
    def run(self, goal: str, entry_path: str) -> DiscoveryResult:
        for m in re.findall(r"\b\d{5,}\b", goal):          # ids the user handed us: visible to the model, never on disk
            self.redactor.register(m, "[goal_id]", model_visible=True)
        self.log.event("discovery_start", f"goal: {goal}", actor="agent", model=self.model.name, entry=entry_path,
                       max_steps=self.max_steps, policy_origins=self.policy.allowed_origins)
        self.s.goto(self.base + entry_path)
        trace: list[TraceStep] = []
        history: list[str] = []
        usage = {"in": 0, "out": 0}
        bad_streak, recent, human_assisted = 0, [], False
        finish: dict[str, Any] = {}
        stop, summary, success = "max_steps", "step budget exhausted", False
        obs = self.s.observe()
        n = 0
        while n < self.max_steps:
            n += 1
            obs = self.s.observe()
            user = (f"GOAL: {goal}\n\nSTEP {n} of at most {self.max_steps}\n\nYOUR ACTION HISTORY:\n"
                    + ("\n".join(history) or "(none yet)") + "\n\nCURRENT SCREEN:\n" + render(obs, self.redactor))
            image = self.s.screenshot() if self.vision else None
            turn = self.model.decide(SYSTEM, user, TOOLS, image_png=image, obs=obs)
            for k in usage:
                usage[k] += turn.usage.get(k, 0)
            if self.token_budget and usage["in"] + usage["out"] > self.token_budget:
                stop, summary = "token_budget", f"stopped: used {usage['in'] + usage['out']} tokens (budget {self.token_budget})"
                self.log.event("token_budget", summary, actor="system", tokens=usage)
                break
            call = turn.call
            if call is None:
                history.append(f"{n}. (no tool call returned)")
                bad_streak += 1
                if bad_streak >= 4:
                    stop, summary = "model_not_calling_tools", "model repeatedly returned no tool call"
                    break
                continue
            rationale = str(call.args.get("rationale", ""))
            self.log.event("agent_step", rationale, actor="agent", step=str(n), tool=call.name,
                           args={k: v for k, v in call.args.items() if k not in ("rationale",)},
                           url=[f.url for f in obs.frames if f.name != "hdr"][-1:], tokens=turn.usage)
            if call.name == "finish":
                finish = call.args
                success = bool(finish.get("success"))
                stop, summary = ("finished" if success else "gave_up"), str(finish.get("summary", ""))
                break
            feedback, ok, step = self._dispatch(n, call, obs, rationale)
            if step is not None:
                trace.append(step)
            if step is not None and step.human:
                human_assisted = True
            history.append(f"{n}. {self._describe(call, obs)} -> {feedback}")
            self.log.shot(f"step-{n}", self.s.screenshot())
            bad_streak = 0 if ok else bad_streak + 1
            sig = (self.s.observe().signature(), call.name, str(call.args.get("ref")))
            recent = (recent + [sig])[-4:]
            if bad_streak >= 4 or (len(recent) == 4 and len(set(recent)) == 1):
                reason = "no_progress" if bad_streak < 4 else "repeated_failures"
                took = self._escalate(reason, f"Agent looks stuck ({reason}) while pursuing: {goal}", n, obs)
                if took is None or took == "abort":
                    stop, summary = "stuck", f"stopped: {reason}"
                    break
                human_assisted, bad_streak, recent = True, 0, []
                history.append(f"{n}. (a human operator intervened; re-read the screen)")
        final_obs = self.s.observe()
        self.log.event("discovery_end", summary, actor="agent", success=success, stop_reason=stop, steps=n,
                       tokens=usage)
        return DiscoveryResult(success, stop, summary, trace, finish, final_obs, n, usage, human_assisted)

    # ------------------------------------------------------------------ tool dispatch
    def _dispatch(self, n: int, call: ToolCall, obs: Observation, rationale: str):
        a = call.args
        fb = {f.name: f.url for f in obs.frames}
        try:
            if call.name in ("click", "type_text", "select_option", "set_checked"):
                return self._element_action(n, call, obs, rationale, fb)
            if call.name == "press_key":
                self.s.perform(ActionType.press, None, a["key"])
                return "pressed", True, self._step(n, ActionType.press, None, obs, a["key"], fb, Risk.reversible, rationale)
            if call.name == "goto":
                url = self.base + a["path"]
                self.policy.check_step_url(url)
                self.s.goto(url)
                return "navigated", True, self._step(n, ActionType.navigate, None, obs, a["path"], fb, Risk.safe, rationale)
            if call.name == "extract_value":
                return self._extract(n, a, obs, rationale, fb)
            if call.name == "ask_human":
                took = self._escalate("model_requested_help", str(a.get("reason", "")), n, obs)
                if took is None:
                    return "no operator available; continue on your own or finish(success=false)", False, None
                return "a human operator intervened; re-read the screen", True, None
            return f"unknown tool {call.name}", False, None
        except PolicyViolation as exc:
            self.log.event("policy_block", str(exc), actor="agent", step=str(n), tool=call.name)
            return f"BLOCKED by policy: {exc}", False, None
        except Exception as exc:  # an action that fails is feedback, not a crash
            self.log.event("action_error", f"{type(exc).__name__}: {str(exc)[:200]}", actor="agent", step=str(n))
            return f"FAILED: {type(exc).__name__}: {str(exc)[:160]}", False, None

    def _element_action(self, n, call, obs, rationale, fb):
        a = call.args
        el = next((e for e in obs.elements if e.ref == a.get("ref")), None)
        if el is None:
            return f"no control with ref {a.get('ref')} on this screen", False, None
        action = {"click": ActionType.click, "type_text": ActionType.fill,
                  "select_option": ActionType.select, "set_checked": ActionType.check}[call.name]
        value: Any = {"click": None, "type_text": a.get("text"), "select_option": a.get("option"),
                      "set_checked": a.get("checked")}[call.name]
        risk, why = self.policy.classify(action, name=el.name or el.text, form_action=el.form_action,
                                         href=el.href, input_type=el.type)
        if risk == Risk.forbidden:
            self.log.event("policy_block", f"forbidden: {why}", actor="agent", step=str(n), control=el.brief())
            return f"BLOCKED by policy ({why}). Do not retry this control.", False, None
        approved = False
        if risk == Risk.irreversible:
            took = self._escalate("needs_approval",
                                  f"Agent wants to perform an IRREVERSIBLE action: {el.role} '{el.name or el.text}' ({why}). "
                                  f"'Retry the step' = approve; 'I completed the step' = you did it; Abort = deny.", n, obs)
            approved = took == "retry_step"
            if not approved:
                self.log.event("policy_block", f"irreversible action not approved: {why}", actor="agent",
                               step=str(n), control=el.brief())
                return ("BLOCKED: this is an irreversible action and no human approved it. Do not retry. "
                        "If the goal is already met, call finish; otherwise finish with success=false."), False, None
        self.s.perform(action, el, value)
        step = self._step(n, action, el, obs, None if value is None else str(value), fb, risk, rationale)
        return "ok", True, step

    def _extract(self, n, a, obs, rationale, fb):
        label = str(a["label"])
        if self.redactor.is_sensitive_label(label):
            self.log.event("policy_block", f"extraction of sensitive label '{label}' refused", actor="agent", step=str(n))
            return f"BLOCKED by policy: '{label}' is sensitive data and may not be extracted.", False, None
        raw = read_labeled_value(obs, [label], None)
        if raw is None:
            avail = ", ".join(sorted({l for l, _ in obs.all_fields()})) or "(none)"
            return f"no field labelled '{label}' on this screen. Labels present: {avail}", False, None
        try:
            coerce(raw, ValueType(a["type"]))
        except ValueError as exc:
            return f"value under '{label}' is not a valid {a['type']}: {exc}", False, None
        frame = next((f.name for f in obs.frames if any(l == label for l, _ in f.fields)), None)
        step = self._step(n, ActionType.extract, None, obs, None, fb, Risk.safe, rationale)
        step.extraction = {"name": a["name"], "label": label, "type": a["type"], "description": a["description"],
                           "frame": frame, "value": raw}
        self.log.event("extract", f"captured '{a['name']}'", actor="agent", step=str(n), output=a["name"],
                       value=mask_value(raw))
        return f"ok: captured '{a['name']}' from label '{label}'", True, step

    def _step(self, n, action, el, obs, value, fb_before, risk, rationale) -> TraceStep:
        after = {f.name: f.url for f in self.s.observe().frames}
        return TraceStep(n, action, el, list(obs.elements), value, fb_before, after, risk, rationale)

    def _describe(self, call: ToolCall, obs: Observation) -> str:
        a = call.args
        el = next((e for e in obs.elements if e.ref == a.get("ref")), None)
        tgt = f'[{el.ref}] {el.role} "{el.name or el.text}"' if el else ""
        extra = {"type_text": f' text={a.get("text")!r}', "select_option": f' option={a.get("option")!r}',
                 "set_checked": f' checked={a.get("checked")}', "press_key": f' key={a.get("key")}',
                 "goto": f' path={a.get("path")}', "extract_value": f' {a.get("name")} <- "{a.get("label")}"'
                 }.get(call.name, "")
        return self.redactor.text(f"{call.name} {tgt}{extra}".strip(), model_view=True)

    # ------------------------------------------------------------------ escalation
    def _escalate(self, reason: str, summary: str, n: int, obs: Observation) -> str | None:
        """Returns the operator's resume choice, or None if no human is available."""
        if self.control is None or self.control.operator is None or (
                reason != "needs_approval" and self.on_stuck != "escalate"):
            return None
        ev = {}
        try:
            ev = {"screenshot": self.log.shot(f"escalation-{reason}", self.s.screenshot()),
                  "dom_snapshot": self.log.snapshot(f"escalation-{reason}", self.s.snapshot_text())}
        except Exception:
            pass
        req = InterventionRequest(
            ticket_id=new_ticket_id(), reason=reason, summary=self.redactor.text(summary), run_id=self.log.run_id,
            capability_or_goal="discovery", step_id=str(n), url=self.redactor.text(obs.frames[-1].url if obs.frames else ""),
            context={"observed": render(obs, self.redactor, 300)[:900]}, evidence=ev)
        t = self.control.escalate(self.s, req)
        return t.resume or "abort"
