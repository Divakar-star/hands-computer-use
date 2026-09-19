"""Pure-logic tests: schema contract, locator resolution, conditions, policy, redaction, overrides.
No browser needed - this is the seam that makes the design testable."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hands.matching import coerce, eval_condition, glob_to_regex, read_labeled_value, resolve
from hands.observation import Element, FrameState, Observation
from hands.policy import Policy, load_policy
from hands.recorder import RecordingError, canonical_digest, load_capability, save_capability
from hands.redact import Redactor, mask_value
from hands.replay import validate_inputs
from hands.schema import (ActionType, Capability, Condition, Locator, Param, Risk, Sensitivity, Target, ValueType)
from hands.tenancy import StaleOverride, StepPatch, TenantOverride, apply_override

ROOT = Path(__file__).resolve().parents[1]


def el(ref, role, name, frame="body", **kw):
    return Element(ref=ref, frame=frame, tag="x", role=role, name=name, **kw)


def obs(elements, text="", fields=(), url="http://h/msc/x", status=200, frame="body"):
    return Observation(frames=[FrameState(frame, url, status, "", text, list(fields))], elements=elements)


# ------------------------------------------------------------------ schema
def test_sensitive_param_cannot_carry_an_example():
    with pytest.raises(ValidationError):
        Param(name="member_id", description="d", sensitivity=Sensitivity.identifier, example="12345")


def _minimal(**over):
    from hands.schema import AppRef, OutputSpec, ReviewSummary, Step, Value
    base = dict(id="msc.x", version="1.0.0", name="x", description="d", app=AppRef(product="msc"), entry="/msc/main",
                inputs=[Param(name="member_id", description="d", sensitivity=Sensitivity.identifier)],
                outputs=[OutputSpec(name="bal", type=ValueType.decimal, description="d")],
                steps=[Step(id="s1", intent="i", action=ActionType.fill, value=Value(param="member_id"),
                            target=Target(description="t", role="textbox",
                                          locators=[Locator(kind="field_name", value="f")]))],
                success=Condition(kind="text_contains", value="x"), review=ReviewSummary(max_risk=Risk.safe))
    base.update(over)
    return base


def test_declared_output_must_be_extracted_by_some_step():
    with pytest.raises(ValidationError, match="no step extracts"):
        Capability(**_minimal())


def test_step_cannot_reference_undeclared_param():
    from hands.schema import Step, Value
    bad = Step(id="s1", intent="i", action=ActionType.fill, value=Value(param="nope"),
               target=Target(description="t", role="textbox", locators=[Locator(kind="field_name", value="f")]))
    with pytest.raises(ValidationError, match="unknown param"):
        Capability(**_minimal(steps=[bad], outputs=[]))


def test_value_needs_exactly_one_of_param_or_literal():
    from hands.schema import Value
    with pytest.raises(ValidationError):
        Value()
    with pytest.raises(ValidationError):
        Value(param="a", literal="b")


def test_tool_spec_exposes_contract_for_calling_agents():
    from hands.schema import AppRef, Extraction, OutputSpec, ReviewSummary, Step, Value
    steps = [Step(id="s1", intent="i", action=ActionType.fill, value=Value(param="member_id"),
                  target=Target(description="t", role="textbox", locators=[Locator(kind="field_name", value="f")])),
             Step(id="s2", intent="r", action=ActionType.extract,
                  extractions=[Extraction(output="bal", labels=["Bal"])])]
    cap = Capability(**_minimal(steps=steps))
    spec = cap.to_tool_spec()
    assert spec["name"] == "msc__x" and spec["input_schema"]["required"] == ["member_id"]
    assert "bal" in spec["returns"]["outputs"] and spec["max_risk"] == "safe"


# ------------------------------------------------------------------ resolution strategy cascade
def target(*locs, frame="body"):
    return Target(description="t", role="link", frame=frame, locators=list(locs))


def test_first_unique_strategy_wins_and_no_drift_when_primary_matches():
    o = obs([el(1, "link", "Member Inquiry", href="/msc/inq")])
    r = resolve(target(Locator(kind="href", value="/msc/inq"), Locator(kind="role_name", role="link", name="Member Inquiry")), o, {})
    assert r.status == "ok" and r.used.kind == "href" and r.drift == []


def test_fallback_is_used_and_reported_as_drift():
    o = obs([el(1, "link", "Find")])
    r = resolve(target(Locator(kind="role_name", role="link", name="Search"),
                       Locator(kind="role_name", role="link", name="Find")), o, {})
    assert r.status == "ok" and r.drift[0][0] == "locator_fallback"


def test_ambiguous_match_is_never_guessed():
    o = obs([el(1, "link", "Share Savings"), el(2, "link", "Share Savings")])
    r = resolve(target(Locator(kind="role_name", role="link", name="Share Savings")), o, {})
    assert r.status == "ambiguous" and r.element is None


def test_strategy_disagreement_is_flagged():
    o = obs([el(1, "link", "A", href="/msc/acct/9-S01"), el(2, "link", "B")])
    r = resolve(target(Locator(kind="href", value="/msc/acct/9-S01"), Locator(kind="role_name", role="link", name="B")), o, {})
    assert r.element.ref == 1 and any(k == "strategy_disagreement" for k, _ in r.drift)


def test_href_locator_substitutes_params_and_ignores_origin():
    o = obs([el(1, "link", "x", href="http://h/msc/acct/12345-S01")])
    r = resolve(target(Locator(kind="href", value="/msc/acct/{{member_id}}-S01")), o, {"member_id": "12345"})
    assert r.status == "ok"
    assert resolve(target(Locator(kind="href", value="/msc/acct/{{member_id}}-S01")), o, {"member_id": "99999"}).status == "none"


def test_frame_hint_falls_back_to_all_frames_with_drift():
    o = obs([el(1, "link", "Go", frame="body")])
    r = resolve(target(Locator(kind="role_name", role="link", name="Go"), frame="main"), o, {})
    assert r.status == "ok" and r.drift[0][0] == "frame_changed"


def test_radio_options_share_a_field_name_but_are_told_apart_by_label():
    o = obs([el(1, "radio", "Existing share draft", name_attr="o_src"), el(2, "radio", "Cash", name_attr="o_src")])
    r = resolve(target(Locator(kind="field_name", role="radio", value="o_src", name="Cash")), o, {})
    assert r.element.ref == 2


# ------------------------------------------------------------------ conditions / extraction
def test_conditions_compose():
    o = obs([], text="No member record found for Member Number 99999.", url="http://h/msc/inq/find")
    yes = Condition(kind="text_contains", value="no member record found")
    assert eval_condition(yes, o, {})
    assert eval_condition(Condition(kind="all", of=[yes, Condition(kind="frame_url", frame="body", value="/msc/inq/*")]), o, {})
    assert not eval_condition(Condition(kind="not", of=[yes]), o, {})
    assert not eval_condition(Condition(kind="any", of=[]), o, {})
    assert eval_condition(Condition(kind="status_in", statuses=[200]), o, {})


def test_frame_url_ignores_query_and_substitutes_params():
    o = obs([], url="http://h/msc/member/12345?x=1")
    assert eval_condition(Condition(kind="frame_url", frame="body", value="/msc/member/{{member_id}}"), o, {"member_id": "12345"})


def test_labeled_value_and_coercion():
    o = obs([], fields=[("Available Balance", "$4,721.37"), ("Status", "Active")])
    assert read_labeled_value(o, ["avail balance", "available balance"], None) == "$4,721.37"
    assert coerce("$4,721.37", ValueType.decimal) == "4721.37"      # decimal as string: no float error
    assert coerce("($12.00)", ValueType.decimal) == "-12.00"
    assert coerce("-$3.50", ValueType.decimal) == "-3.50"
    assert coerce("7", ValueType.integer) == 7
    with pytest.raises(ValueError):
        coerce("n/a", ValueType.decimal)


def test_glob():
    assert glob_to_regex("/msc/acct/{{m}}-S*", {"m": "1"}).match("/msc/acct/1-S02")


# ------------------------------------------------------------------ input validation (fails before touching the app)
def test_validate_inputs_rejects_bad_and_unknown():
    cap = Capability(**_minimal(steps=_minimal()["steps"] + [__import__("hands.schema", fromlist=["Step"]).Step(
        id="s2", intent="r", action=ActionType.extract,
        extractions=[__import__("hands.schema", fromlist=["Extraction"]).Extraction(output="bal", labels=["Bal"])])]))
    cap.inputs[0].pattern = r"\d{5}"
    _, errs = validate_inputs(cap, {"member_id": "12ab", "extra": "x"})
    assert any("format" in e for e in errs) and any("unknown input" in e for e in errs)
    ok, errs = validate_inputs(cap, {"member_id": "12345"})
    assert not errs and ok == {"member_id": "12345"}
    assert validate_inputs(cap, {})[1] == ["missing required input 'member_id'"]


# ------------------------------------------------------------------ policy
POLICY = load_policy(ROOT / "policies" / "msc.policy.json")


@pytest.mark.parametrize("name,form,href,expected", [
    ("Search", None, "javascript:void(0)", Risk.safe),
    ("Sign Off", None, "/msc/logoff", Risk.forbidden),
    ("Confirm and Submit", "/msc/open/commit", None, Risk.irreversible),
    ("Approve wire", None, None, Risk.irreversible),          # unknown app: name heuristic is fail-safe
    ("Continue >>", "/msc/open/review", None, Risk.safe),
])
def test_click_risk_classification(name, form, href, expected):
    assert POLICY.classify(ActionType.click, name=name, form_action=form, href=href)[0] == expected


def test_typing_is_reversible_and_passwords_are_forbidden():
    assert POLICY.classify(ActionType.fill, name="Member Number", input_type="text")[0] == Risk.reversible
    assert POLICY.classify(ActionType.fill, name="Password", input_type="password")[0] == Risk.forbidden


def test_disallowed_action_type_is_forbidden():
    p = Policy(allowed_origins=["http://h"], allowed_actions=[ActionType.click])
    assert p.classify(ActionType.fill, name="x")[0] == Risk.forbidden


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8765/msc/inq", True),
    ("http://127.0.0.1:8765/__admin/faults", False),
    ("http://127.0.0.1:8765/msc/logoff", False),
    ("http://127.0.0.1:8765/other", False),
    ("https://evil.example/msc/inq", False),
    ("about:blank", True),
])
def test_allowlist(url, ok):
    assert POLICY.url_allowed(url)[0] is ok


# ------------------------------------------------------------------ redaction
def test_pattern_learned_and_registered_redaction():
    r = Redactor(POLICY.sensitive_labels)
    r.register("hunter2-pass", "[SECRET]")
    r.learn_fields([("Name", "Dana Whitfield"), ("SSN / TIN", "900-12-3456"), ("Phone", "(555) 010-0142")])
    out = r.text("Dana Whitfield ssn 900-12-3456 pw hunter2-pass tel (555) 010-0142 mail a.b@c.io")
    assert "Dana" not in out and "900-12" not in out and "hunter2" not in out and "010-0142" not in out and "a.b@c.io" not in out


def test_card_numbers_need_luhn_so_ids_and_timestamps_survive():
    r = Redactor()
    assert "[CARD]" in r.text("4111 1111 1111 1111")
    assert r.text("run-20260918-170849-831") == "run-20260918-170849-831"


def test_model_visible_values_are_hidden_only_from_persistence():
    r = Redactor()
    r.register("12345", "[member_id]", model_visible=True)
    assert r.text("member 12345", model_view=True) == "member 12345"
    assert r.text("member 12345") == "member [member_id]"


def test_financial_output_mask_keeps_shape_only():
    assert mask_value("$4,721.37") == "$*,***.37"


# ------------------------------------------------------------------ artifact integrity + tenancy
def _saved(tmp_path):
    from hands.schema import Extraction, Step
    steps = _minimal()["steps"] + [Step(id="s2", intent="r", action=ActionType.extract,
                                        extractions=[Extraction(output="bal", labels=["Bal"])])]
    cap = Capability(**_minimal(steps=steps))
    return cap, save_capability(cap, tmp_path / "c.json")


def test_digest_detects_edits_after_save(tmp_path):
    cap, path = _saved(tmp_path)
    assert load_capability(path).digest == canonical_digest(cap)
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["steps"][0]["risk"] = "safe"
    tampered["steps"][0]["intent"] = "totally different"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RecordingError, match="digest mismatch"):
        load_capability(path)


def test_override_only_adds_and_pins_the_base(tmp_path):
    cap, _ = _saved(tmp_path)
    ov = TenantOverride(tenant="lakeside", capability_id=cap.id, base_version=cap.version, base_digest=cap.digest,
                        step_patches={"s1": StepPatch(add_locators=[Locator(kind="role_name", role="textbox", name="Cust. #")]),
                                      "s2": StepPatch(add_extraction_labels={"bal": ["Avail Bal"]})})
    out = apply_override(cap, ov)
    assert [l.kind for l in out.steps[0].target.locators] == ["field_name", "role_name"]   # appended = fallback
    assert out.steps[1].extractions[0].labels == ["Bal", "Avail Bal"]
    assert len(cap.steps[0].target.locators) == 1                                          # base untouched
    stale = ov.model_copy(update={"base_version": "9.9.9"})
    with pytest.raises(StaleOverride):
        apply_override(cap, stale)


# ------------------------------------------------------------------ attestation rule (found in the real-model run)
def test_consent_checkbox_can_never_be_baked_in_as_a_constant():
    from hands.agent import TraceStep
    from hands.recorder import Recorder
    box = Element(ref=1, frame="body", tag="input", role="checkbox", type="checkbox", name_attr="o_cns",
                  name="Member consent and disclosures provided", group="Disclosures")
    t = TraceStep(n=1, action=ActionType.check, element=box, snapshot=[box], value="True", frames_before={},
                  frames_after={}, risk=Risk.reversible, rationale="needed to proceed")
    rec = Recorder(POLICY, Redactor(POLICY.sensitive_labels), {"product": "msc"})
    meta: dict = {}
    step = rec._compile_step("s1", t, {}, {}, meta)          # the model declared NO parameter for it
    assert step.value.param == "member_consent_and_disclosures_provided" and step.value.literal is None
    params = rec._inputs([], meta)
    assert params[0].type.value == "boolean" and params[0].required and "attests" in params[0].description

    plain = Element(ref=2, frame="body", tag="input", role="radio", type="radio", name_attr="o_src",
                    name="Existing share draft", group="Funding Source")
    t2 = TraceStep(n=2, action=ActionType.check, element=plain, snapshot=[plain], value="True", frames_before={},
                   frames_after={}, risk=Risk.reversible, rationale="funding")
    assert rec._compile_step("s2", t2, {}, {}, {}).value.literal is True     # ordinary choices stay constants


def test_checkbox_is_bound_by_its_label_never_by_the_value_true():
    """Regression from the real-model run: the funding-source radio was bound to the consent parameter
    because both were ticked ('true'). Only a label that relates to a declared boolean may bind."""
    from hands.agent import TraceStep
    from hands.recorder import Recorder
    rec = Recorder(POLICY, Redactor(POLICY.sensitive_labels), {"product": "msc"})
    rec._declared = [{"name": "consent_confirmed", "type": "boolean", "example_value": "true",
                      "description": "Member consent to the disclosures was obtained"}]
    by_value = {"true": "consent_confirmed"}                     # what the old code keyed on
    radio = Element(ref=1, frame="body", tag="input", role="radio", type="radio", name_attr="o_src",
                    name="Existing share draft", group="Funding Source")
    box = Element(ref=2, frame="body", tag="input", role="checkbox", type="checkbox", name_attr="o_cns",
                  name="Member consent and disclosures provided", group="Disclosures")
    def trace(e):
        return TraceStep(n=1, action=ActionType.check, element=e, snapshot=[radio, box], value="True",
                         frames_before={}, frames_after={}, risk=Risk.reversible, rationale="")
    meta: dict = {}
    assert rec._compile_step("s1", trace(radio), {}, by_value, meta).value.literal is True     # stays a constant
    consent = rec._compile_step("s2", trace(box), {}, by_value, meta)
    assert consent.value.param == "consent_confirmed"                                          # related label binds
