"""Compile a successful discovery trace into a Capability artifact.

The model transcript is thrown away; what survives is the *flow*. The recorder is
where robustness is decided, deterministically and checkably:

  * needed steps: the model says which trace steps were required; dead ends drop out.
  * parameterisation: literals equal to a declared example value become {{param}}
    references - in typed values, in href locators and in URL checkpoints - so the
    artifact contains no concrete member data.
  * locators: for each control, candidate strategies are generated from its
    descriptor and each is kept only if it resolves to *exactly that control* on the
    screen it was recorded on. Ordered by stability; the ordering rationale is stored.
  * checkpoints: per-step postconditions are derived from what actually changed
    (which frame's URL) and the final success condition from the last screen.
  * non-happy paths: product-wide knowledge (profile) + conditional steps the model
    declared become outcomes / recoverables / failure signatures inside the artifact,
    so the artifact stays self-contained and reviewable.

Anything that would embed sensitive data (a locator string that redacts, a literal that
looks like PII) aborts compilation rather than being silently saved.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agent import DiscoveryResult, TraceStep
from .matching import locator_matches
from .observation import Element
from .policy import Policy
from .redact import Redactor
from .schema import (ActionType, AppRef, Capability, Condition, Extraction, FailureSignature, Locator, Outcome,
                     OutputSpec, Param, Provenance, Recoverable, ReviewSummary, Risk, Sensitivity, Step, Target,
                     Value, ValueType, max_risk)


class RecordingError(Exception):
    pass


_FIELD_ROLES = {"textbox", "combobox", "checkbox", "radio"}
_GENERIC_WORDS = {"member", "caller", "that", "this", "with", "have", "been", "provided", "value", "true", "false"}
_ATTESTATION = re.compile(r"consent|disclos|attest|certif|authori[sz]|\bagree", re.I)


def canonical_digest(cap: Capability) -> str:
    body = cap.model_dump(mode="json", exclude={"digest", "verification", "status"}, exclude_none=True)
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save_capability(cap: Capability, path: str | Path) -> Path:
    cap.digest = canonical_digest(cap)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cap.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    return p


def load_capability(path: str | Path, verify_digest: bool = True) -> Capability:
    cap = Capability.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if verify_digest and cap.digest and cap.digest != canonical_digest(cap):
        raise RecordingError(f"{path}: digest mismatch - artifact was modified after it was saved")
    return cap


def _path(url: str) -> str:
    return urlparse(url).path


class Recorder:
    def __init__(self, policy: Policy, redactor: Redactor, profile: dict[str, Any]):
        self.policy, self.redactor, self.profile = policy, redactor, profile
        self.last_examples: dict[str, str] = {}   # normalised example inputs, used to verify by replay

    # ================================================================== compile
    def compile(self, d: DiscoveryResult, *, goal: str, model: str, run_id: str, tenant: str | None,
                product_version: str | None, transcript_ref: str | None, cap_id: str | None = None) -> Capability:
        if not d.success:
            raise RecordingError("discovery did not succeed; nothing to record")
        fin = d.finish
        params_decl = fin.get("parameters") or []
        self._declared = params_decl
        examples = {p["name"]: str(p["example_value"]) for p in params_decl if p.get("example_value") not in (None, "")}
        by_value = {v: k for k, v in examples.items()}

        ok_steps = [t for t in d.trace if t.ok]
        needed = set(fin.get("needed_steps") or [])
        chosen = [t for t in ok_steps if not needed or t.n in needed or t.action == ActionType.extract]
        if not chosen:
            raise RecordingError("no recordable steps")
        conditional = {int(c["step"]): c for c in (fin.get("conditional_steps") or [])}

        steps: list[Step] = []
        outcomes: list[Outcome] = []
        outputs: list[OutputSpec] = []
        param_meta: dict[str, dict[str, Any]] = {}
        for idx, t in enumerate(chosen, start=1):
            step = self._compile_step(f"s{idx}", t, examples, by_value, param_meta)
            if t.n in conditional:
                c = conditional[t.n]
                step.on_target_missing = self._code(c["outcome_code"])
                outcomes.append(self._conditional_outcome(c))
            if t.action == ActionType.extract:
                ex = t.extraction or {}
                sens = Sensitivity.financial
                outputs.append(OutputSpec(name=ex["name"], type=ValueType(ex["type"]),
                                          description=ex["description"], sensitivity=sens))
            steps.append(step)

        self.last_examples = {**examples, **{k: m["example_norm"] for k, m in param_meta.items() if "example_norm" in m}}
        inputs = self._inputs(params_decl, param_meta)
        used = {s.value.param for s in steps if s.value and s.value.param}
        inputs = [p for p in inputs if p.name in used or p.name in self._href_params(steps)]
        outcomes = self._profile_outcomes() + outcomes
        cap_slug = re.sub(r"[^a-z0-9_]+", "_", (cap_id or (fin.get("capability") or {}).get("slug") or "capability").lower()).strip("_")
        product = self.profile["product"]
        max_r = max_risk(*[s.risk for s in steps])
        cap = Capability(
            id=f"{product}.{cap_slug}" if "." not in cap_slug else cap_slug,
            version="1.0.0",
            status="draft",
            name=cap_slug.replace("_", " "),
            description=self.redactor.text((fin.get("capability") or {}).get("description") or fin.get("summary", "")),
            app=AppRef(product=product, product_version_seen=product_version, tenant_recorded_on=tenant),
            entry=self.profile.get("entry", "/"),
            inputs=inputs, outputs=outputs, steps=steps,
            success=self._success(d, steps, fin),
            outcomes=outcomes,
            recoverables=[Recoverable.model_validate(r) for r in self.profile.get("recoverables", [])],
            failure_signatures=[FailureSignature.model_validate(f) for f in self.profile.get("failure_signatures", [])],
            review=ReviewSummary(
                max_risk=max_r, irreversible_steps=[s.id for s in steps if s.risk == Risk.irreversible],
                reads=[o.name for o in outputs],
                notes=["business outcomes/recoverables merged from the product profile "
                       f"'{product}' at record time; artifact is self-contained",
                       "structural (ordinal) locators are last-resort and only kept when nothing sturdier is unique"]
                + (["discovery was human-assisted: review before approving"] if d.human_assisted else [])),
            provenance=Provenance(run_id=run_id, recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  goal=self.redactor.text(goal), model=model, discovery_steps=d.steps_used,
                                  human_assisted=d.human_assisted, transcript_ref=transcript_ref),
        )
        self._assert_clean(cap)
        cap.digest = canonical_digest(cap)
        return cap

    # ================================================================== steps
    def _compile_step(self, sid: str, t: TraceStep, examples, by_value, meta) -> Step:
        val: Value | None = None
        if t.action in (ActionType.fill, ActionType.select, ActionType.check, ActionType.navigate, ActionType.press):
            orig = t.value if t.value is not None else ""
            raw = orig
            if t.action == ActionType.select and t.element:
                # dropdowns are recorded by option VALUE, not visible text: text is tenant wording
                raw = next((o["value"] for o in t.element.options if orig in (o["value"], o["text"])), orig)
            if t.action == ActionType.navigate:
                val = Value(literal=self._param_url(raw, by_value))
            else:
                label = (t.element.name or t.element.text) if t.element else ""
                if t.action == ActionType.check:
                    # Matching a checkbox to a parameter by its VALUE ("true") is meaningless: every ticked box has
                    # it. (A real run bound an unrelated funding-source radio to the consent parameter that way.)
                    # Bind only when the control's label actually relates to a declared boolean parameter.
                    pname = self._match_bool_param(label)
                else:
                    pname = next((by_value[c] for c in (orig, raw, orig.lower(), raw.lower()) if c in by_value), None)
                if pname is not None:
                    val = Value(param=pname)
                    m = meta.setdefault(pname, {})
                    m["example_norm"] = raw.lower() if t.action == ActionType.check else raw
                    if t.action == ActionType.select and t.element:
                        m["enum"] = [o["value"] for o in t.element.options if o["value"]]
                    if t.action == ActionType.check:
                        m["type"] = "boolean"
                else:
                    if t.action == ActionType.check and raw.lower() == "true" and _ATTESTATION.search(label):
                        # A consent / disclosure / certification box is an ATTESTATION by a person. A recorded
                        # capability must never make it on the caller's behalf on every future call: it becomes a
                        # required boolean input the caller has to supply, whatever the model declared.
                        pname = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:48] or "attestation"
                        val = Value(param=pname)
                        m = meta.setdefault(pname, {})
                        m.update(type="boolean", example_norm="true",
                                 auto_desc=f"Caller attests: '{label}'. The capability never asserts this on the caller's behalf.")
                    else:
                        val = Value(literal=(raw.lower() == "true") if t.action == ActionType.check else raw)
                        if isinstance(val.literal, str) and self.redactor.changed(val.literal):
                            raise RecordingError(f"step {sid}: literal input looks sensitive; declare it as a parameter")
        target = None
        if t.action in (ActionType.click, ActionType.fill, ActionType.select, ActionType.check):
            assert t.element is not None
            target = self._target(t.element, t.snapshot, examples, by_value, t.action)
        expect = self._expect(t, by_value) if t.action in (ActionType.click, ActionType.navigate, ActionType.press) else None
        extractions: list[Extraction] = []
        if t.action == ActionType.extract:
            ex = t.extraction or {}
            extractions = [Extraction(output=ex["name"], labels=[ex["label"]], frame=ex.get("frame"))]
        risk, _ = self.policy.classify(t.action, name=(t.element.name or t.element.text) if t.element else "",
                                       form_action=t.element.form_action if t.element else None,
                                       href=t.element.href if t.element else None,
                                       input_type=t.element.type if t.element else None)
        return Step(id=sid, intent=self._intent(t), action=t.action, target=target, value=val,
                    risk=max_risk(risk, t.risk), expect=expect, extractions=extractions)

    def _match_bool_param(self, label: str) -> str | None:
        """A declared boolean parameter whose name/description shares a significant word with the control's label."""
        words = lambda s: {w for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in _GENERIC_WORDS}
        want = words(label)
        for p in getattr(self, "_declared", []):
            if p.get("type") == "boolean" and want & words(p["name"] + " " + p.get("description", "")):
                return p["name"]
        return None

    def _intent(self, t: TraceStep) -> str:
        if t.action == ActionType.extract and t.extraction:
            return f"Read '{t.extraction['label']}' as {t.extraction['name']}"
        label = (t.element.name or t.element.text) if t.element else (t.value or "")
        verb = {"click": "Click", "fill": "Enter", "select": "Choose", "check": "Set", "navigate": "Go to",
                "press": "Press"}[t.action.value]
        return self.redactor.text(f"{verb} {label}".strip())

    # ------------------------------------------------------------------ targets
    def _target(self, el: Element, snapshot: list[Element], examples: dict[str, str], by_value, action) -> Target:
        cands: list[Locator] = []
        if el.name_attr and el.role in _FIELD_ROLES:
            cands.append(Locator(kind="field_name", role=el.role, value=el.name_attr,
                                 name=el.name if el.role == "radio" else None, stability="high",
                                 note="server-side field name: survives relabelling / tenant wording"))
        if el.href and not el.href.startswith("javascript"):
            cands.append(Locator(kind="href", value=self._param_url(el.href, by_value), stability="high",
                                 note="link destination with parameters templated: independent of visible label"))
        if el.name or el.text:
            cands.append(Locator(kind="role_name", role=el.role, name=el.name or el.text, group=el.group or None,
                                 stability="medium", note="what a human sees: role + accessible name (incl. adjacent label cell)"))
        if el.role in ("link", "button") and el.text and el.text != el.name:
            cands.append(Locator(kind="visible_text", role=el.role, name=el.text, stability="medium"))
        cands.append(Locator(kind="structural", role=el.role, ordinal=el.ordinal, stability="low",
                             note="position among same-role controls: last resort only"))

        def unique(loc: Locator) -> bool:
            hits = [e for e in snapshot if e.frame == el.frame and locator_matches(loc, e, examples)]
            return len(hits) == 1 and hits[0].ref == el.ref

        good = [c for c in cands if unique(c)]
        good = [c for c in good if not self._locator_sensitive(c)]
        if not good:
            raise RecordingError(f"no unique, non-sensitive locator for control {el.brief()!r}")
        strong = [c for c in good if c.stability != "low"]
        final = strong or good          # low-stability structural only if nothing sturdier is unique
        rank = {"high": 0, "medium": 1, "low": 2}
        final = sorted(final, key=lambda c: rank[c.stability])
        why = ("ordered by resilience: machine identifiers (field name / href) first because they survive "
               "relabelling across tenants; then role+name; positional last. Every listed locator was verified "
               f"unique on the recorded screen; dropped as non-unique: {[c.kind for c in cands if c not in good]}")
        return Target(description=self.redactor.text(f"{el.role} '{el.name or el.text}'" + (f" in group '{el.group}'" if el.group else "")),
                      frame=el.frame, role=el.role, locators=final, robustness=why)

    def _locator_sensitive(self, loc: Locator) -> bool:
        return any(v and self.redactor.changed(v) for v in (loc.name, loc.value, loc.group))

    def _param_url(self, raw: str, by_value: dict[str, str]) -> str:
        out = re.sub(r"^https?://[^/]+", "", raw)
        for value, pname in sorted(by_value.items(), key=lambda kv: -len(kv[0])):
            if value and len(value) >= 3:
                out = out.replace(value, "{{" + pname + "}}")
        return out

    # ------------------------------------------------------------------ checkpoints
    def _expect(self, t: TraceStep, by_value) -> Condition | None:
        changed = [(f, u) for f, u in t.frames_after.items() if t.frames_before.get(f) != u]
        non_top = [(f, u) for f, u in changed if f != "top" or len(t.frames_after) == 1]
        pick = non_top[-1] if non_top else (changed[-1] if changed else None)
        if pick is None:
            return None
        frame, url = pick
        return Condition(kind="frame_url", frame=frame, value=self._param_url(_path(url), by_value),
                         describe=f"frame '{frame}' lands on {self._param_url(_path(url), by_value)}")

    def _success(self, d: DiscoveryResult, steps: list[Step], fin: dict[str, Any]) -> Condition:
        parts: list[Condition] = []
        obs = d.final_obs
        assert obs is not None
        last_nav = next((s for s in reversed(steps) if s.expect and s.expect.kind == "frame_url"), None)
        if last_nav is not None:
            parts.append(last_nav.expect)  # type: ignore[arg-type]
        # Structural evidence first (URL, and the extraction steps which already require their labels to
        # be present and parseable). Static text is tenant wording, so it is used only when nothing
        # structural proves the goal state (e.g. reaching a review screen with no data read).
        has_extraction = any(s.extractions for s in steps)
        if not has_extraction:
            text = re.sub(r"\s+", " ", obs.text()).casefold()
            for ev in fin.get("success_evidence") or []:
                if ev and ev.casefold() in text and not self.redactor.changed(ev):
                    parts.append(Condition(kind="text_contains", value=ev))
        if not parts:
            raise RecordingError("could not derive a success checkpoint (no URL change, evidence or extraction)")
        return Condition(kind="all", of=parts, describe="final screen matches the recorded goal state")

    # ------------------------------------------------------------------ contract
    def _inputs(self, decl: list[dict[str, Any]], meta) -> list[Param]:
        out = []
        for p in decl:
            sens = Sensitivity(p.get("sensitivity", "none"))
            if sens == Sensitivity.none and self.redactor.changed(str(p.get("example_value", ""))):
                sens = Sensitivity.identifier      # the value was registered as sensitive: never keep it as an example
            m = meta.get(p["name"], {})
            ptype = ValueType(m.get("type", p.get("type", "string")))
            pattern = p.get("pattern")
            ex = self.last_examples.get(p["name"], str(p.get("example_value", "")))
            if pattern and not re.fullmatch(pattern, ex):
                pattern = None                 # a model-supplied pattern that rejects its own example is wrong
            out.append(Param(name=p["name"], type=ptype, description=self.redactor.text(p["description"]), required=True,
                             pattern=pattern, enum=m.get("enum"), sensitivity=sens,
                             example=ex if sens == Sensitivity.none else None))
        declared = {p["name"] for p in decl}
        for name, m in meta.items():           # inputs the recorder itself created (attestations)
            if "auto_desc" in m and name not in declared:
                out.append(Param(name=name, type=ValueType.boolean, description=m["auto_desc"], required=True,
                                 example="true"))
        return out

    def _href_params(self, steps: list[Step]) -> set[str]:
        names: set[str] = set()
        for s in steps:
            for loc in (s.target.locators if s.target else []):
                names |= set(re.findall(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", loc.value or ""))
            if s.expect and s.expect.value:
                names |= set(re.findall(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", s.expect.value))
        return names

    def _profile_outcomes(self) -> list[Outcome]:
        return [Outcome.model_validate(o) for o in self.profile.get("outcomes", [])]

    @staticmethod
    def _code(raw: str) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")

    def _conditional_outcome(self, c) -> Outcome:
        """'Control absent' as a business answer, raised by the step's on_target_missing rule
        (no detector of its own: the absence of the control *is* the signal)."""
        desc = self.redactor.text(c["description"])
        return Outcome(code=self._code(c["outcome_code"]), description=desc, caller_guidance=desc)

    def _assert_clean(self, cap: Capability) -> None:
        """Last line of defence: refuse to save an artifact that embeds a registered sensitive
        value or PII-shaped string. ({{param}} placeholders are fine.)"""
        blob = cap.model_dump_json()
        if any(v and v in blob for v in self.redactor.screen_masks()):
            raise RecordingError("artifact contains a registered sensitive value; refusing to save")
        if re.search(r"\b\d{3}-\d{2}-\d{4}\b", blob):
            raise RecordingError("artifact contains an SSN-shaped value; refusing to save")


def load_profile(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

