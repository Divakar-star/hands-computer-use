"""End-to-end scenarios against a REAL browser and the mock console.

A capability is recorded once (scripted stand-in for the model - the real LLM run is a
separate deliverable) and then replayed under each runtime condition the brief lists.
Each test asserts the *category* of response: business outcome vs recovered vs hard failure
vs blocked vs escalated - the distinction the result contract exists to make."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hands.control import ControlViolation, ScriptedOperator
from hands.harness import MockServer, with_commit_step
from hands.pipeline import discover, replay
from hands.policy import load_policy
from hands.schema import ActionType, Capability
from hands.scripts import OPEN_REVIEW_GOAL, SAVINGS_BALANCE_GOAL, open_review_script, savings_balance_script
from hands.surface import PlaywrightSurface
from hands.tenancy import StepPatch, TenantOverride
from hands.schema import Locator

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy(ROOT / "policies" / "msc.policy.json")
PROFILE = ROOT / "profiles" / "msc.json"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scenarios")
    out, caps = tmp / "ev", tmp / "caps"
    with MockServer(port=8791) as prairie, MockServer(tenant="lakeside", port=8792) as lakeside:
        quiet = lambda *_: None
        cap1, _, ver1 = discover(target=prairie.url, goal=SAVINGS_BALANCE_GOAL, model=savings_balance_script(),
                                 policy=POLICY, profile_path=PROFILE, out_dir=out, cap_dir=caps, tenant="prairie", say=quiet)
        cap2, _, ver2 = discover(target=prairie.url, goal=OPEN_REVIEW_GOAL, model=open_review_script(),
                                 policy=POLICY, profile_path=PROFILE, out_dir=out, cap_dir=caps, tenant="prairie", say=quiet)
        assert ver1 and ver1.status == "success" and ver2 and ver2.status == "success", "recording must verify"
        yield SimpleNamespace(srv=prairie, lake=lakeside, cap=cap1, cap2=cap2, out=out)


@pytest.fixture(autouse=True)
def _reset(env):
    env.srv.reset()
    env.lake.reset()


def run(env, cap=None, member="12345", srv=None, **kw):
    srv = srv or env.srv
    return replay(cap or env.cap, {"member_id": member}, target=srv.url, policy=POLICY, out_dir=env.out, **kw)


def events(env, res):
    p = env.out / res.run_id / "events.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]


def fast(cap: Capability, ms=1200) -> Capability:
    c = cap.model_copy(deep=True)
    for s in c.steps:
        s.timeout_ms = ms
    return c


# =============================================================== the happy path & determinism
def test_recorded_artifact_is_self_describing(env):
    c = env.cap
    assert c.status == "verified" and c.verification.passed and c.review.max_risk.value == "reversible"
    assert [i.name for i in c.inputs] == ["member_id"] and c.inputs[0].example is None   # identifiers: no example kept
    assert "12345" not in c.model_dump_json()
    assert [s.action.value for s in c.steps] == ["click", "fill", "click", "click", "extract"]
    assert {o.code for o in c.outcomes} >= {"record_not_found", "access_restricted", "no_savings_account"}


def test_replay_is_deterministic_and_uses_no_model(env):
    outs = [run(env).outputs for _ in range(3)]
    assert outs == [{"available_balance": "4721.37"}] * 3
    res = run(env)
    assert res.status == "success" and res.steps_completed == 5 and res.recoveries == [] and res.drift == []


# =============================================================== business outcomes are answers, not errors
@pytest.mark.parametrize("member,outcome", [
    ("99999", "record_not_found"), ("77777", "access_restricted"), ("40551", "no_savings_account")])
def test_business_outcomes(env, member, outcome):
    res = run(env, member=member)
    assert res.status == "business_outcome" and res.outcome == outcome and res.failure is None


def test_invalid_input_is_rejected_before_touching_the_app(env):
    res = run(env, member="12ab")
    assert res.status == "failed" and res.failure.category.value == "invalid_input"
    assert len(env.srv.state.sessions) == 0          # not even a login happened


# =============================================================== recoverable conditions
def test_compliance_interstitial_is_recovered(env):
    env.srv.set_faults(notice=True)
    res = run(env)
    assert res.status == "success" and res.outputs["available_balance"] == "4721.37"
    assert [r.code for r in res.recoveries] == ["compliance_notice"]


def test_transient_503_is_retried_with_backoff(env):
    env.srv.set_faults(flaky_member=2)
    res = run(env)
    assert res.status == "success" and res.recoveries[0].code == "transient_unavailable" and res.recoveries[0].attempts == 2


def test_session_timeout_mid_run_reauthenticates_and_restarts(env):
    env.srv.set_faults(expire_after=2)
    res = run(env)
    assert res.status == "success" and any(r.code == "session_expired" for r in res.recoveries)


def test_slow_pages_are_waited_for_without_fixed_sleeps(env):
    env.srv.set_faults(slow_ms=1500)
    res = run(env)
    assert res.status == "success" and res.duration_ms >= 3000


# =============================================================== hard failures carry debuggable evidence
def test_persistent_transient_error_becomes_a_hard_failure(env):
    env.srv.set_faults(flaky_member=50)
    res = run(env)
    assert res.status == "failed" and res.failure.category.value == "app_error" and res.failure.retriable
    assert res.failure.step_id == "s3" and "persisted" in res.failure.message and res.failure.evidence["screenshot"]


def test_raw_app_error_stops_with_what_step_expected_observed_and_evidence(env):
    env.srv.set_faults(app_error_acct=True)
    res = run(env)
    f = res.failure
    assert res.status == "failed" and f.category.value == "app_error" and f.step_id == "s4"
    assert "expected" in f.model_dump() and f.expected and "ORA-00942" in f.observed
    assert (env.out / f.evidence["screenshot"]).exists() and (env.out / f.evidence["dom_snapshot"]).exists()


def test_unresolvable_control_reports_strategies_tried(env):
    res = run(env, srv=env.lake)          # different wording, no override
    f = res.failure
    assert res.status == "failed" and f.category.value == "target_not_found" and f.step_id == "s3"
    assert "role_name" in f.expected


# =============================================================== multi-tenant reuse
def test_tenant_override_specialises_without_re_recording(env):
    ov = TenantOverride(tenant="lakeside", capability_id=env.cap.id, base_version=env.cap.version,
                        base_digest=env.cap.digest,
                        step_patches={"s3": StepPatch(add_locators=[Locator(kind="role_name", role="link", name="Find")]),
                                      "s5": StepPatch(add_extraction_labels={"available_balance": ["Avail Balance"]})})
    res = run(env, srv=env.lake, override=ov)
    assert res.status == "success" and res.tenant == "lakeside" and res.outputs["available_balance"] == "4721.37"
    assert [d.kind for d in res.drift] == ["locator_fallback"] and res.drift[0].step_id == "s3"


# =============================================================== safety
def with_commit(cap, declared="safe"):
    return with_commit_step(cap, declared)


def open_inputs():
    return {"member_id": "12345", "account_type": "SAV", "initial_deposit": "25.00",
            "nickname": "Vacation", "member_consent_confirmed": True}


def run_open(env, cap, **kw):
    return replay(cap, open_inputs(), target=env.srv.url, policy=POLICY, out_dir=env.out, **kw)


def test_review_capability_reaches_confirmation_and_commits_nothing(env):
    res = run_open(env, env.cap2)
    assert res.status == "success" and env.srv.commits == []
    assert env.cap2.review.max_risk.value == "reversible" and env.cap2.review.irreversible_steps == []
    assert env.cap2.inputs[1].enum == ["SAV", "MMK"]       # dropdown recorded by option VALUE, not tenant wording


def test_open_form_validation_error_is_a_business_outcome(env):
    bad = {**open_inputs(), "initial_deposit": "2.00"}
    res = replay(env.cap2, bad, target=env.srv.url, policy=POLICY, out_dir=env.out)
    assert res.status == "business_outcome" and res.outcome == "input_rejected" and env.srv.commits == []


def test_runtime_rederives_risk_so_an_understated_artifact_cannot_commit(env):
    cap = with_commit(env.cap2, declared="safe")            # the artifact LIES that the commit is safe
    res = run_open(env, cap)
    assert res.status == "blocked" and res.failure.category.value == "policy_violation" and res.failure.step_id == "s99"
    assert env.srv.commits == []
    assert any(e["type"] == "risk_escalated" for e in events(env, res))


def test_explicit_approval_lets_the_irreversible_step_run(env):
    res = run_open(env, with_commit(env.cap2), approvals={"s99"})
    assert res.status == "success" and len(env.srv.commits) == 1


def test_forbidden_actions_have_no_override(env):
    d = env.cap.model_dump(mode="json")
    d["steps"].insert(0, {"id": "s0", "intent": "sign off", "action": "click",
                          "target": {"description": "Sign Off", "frame": "hdr", "role": "link",
                                     "locators": [{"kind": "role_name", "role": "link", "name": "Sign Off"}]}})
    d["digest"] = None
    res = run(env, Capability.model_validate(d), approvals={"*"})
    assert res.status == "blocked" and "forbidden" in res.failure.message


def test_network_layer_blocks_requests_outside_the_allowlist(env):
    from hands.pipeline import policy_for
    from hands.redact import Redactor
    pol = policy_for(POLICY, env.srv.url)
    with PlaywrightSurface(pol, Redactor()) as s:
        for url in (env.srv.url + "/__admin/faults", "http://127.0.0.1:9/msc/x"):
            with pytest.raises(Exception):
                s.page.goto(url)
        assert len(s.blocked_requests) == 2


def test_no_sensitive_data_reaches_logs_results_or_snapshots(env):
    env.srv.set_faults(app_error_acct=True)                  # forces a DOM snapshot of a page that had PII on the way
    run(env)
    env.srv.reset()
    run(env)
    # regression: the review screen shows '12345 - <Full Name>' in an unlabeled-as-sensitive cell, and the
    # approval ticket persists a DOM snapshot of it. The name must be learned from that format and scrubbed.
    def deny(surface, control):
        control.operator = ScriptedOperator(surface, lambda s: None, control, resume="abort")
    run_open(env, with_commit(env.cap2), surface_hook=deny)
    text = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in env.out.rglob("*")
                   if p.is_file() and p.suffix in (".jsonl", ".json", ".txt"))
    for needle in ("Dana Whitfield", "900-12-3456", "(555) 010-0142", "1420 Elm", "demo-not-a-real-password", "4721.37"):
        assert needle not in text, f"{needle!r} leaked to disk"
    assert list(env.out.rglob("*.png")), "screenshots are captured (masked in-page before capture)"


# =============================================================== human handoff
def test_stuck_replay_hands_the_live_session_to_a_human_and_resumes(env):
    env.srv.set_faults(notice=True)
    cap = fast(env.cap)
    cap.recoverables = []                                    # the notice is now an UNKNOWN state
    seen = []

    def hook(surface, control):
        def human(s):
            with pytest.raises(ControlViolation):            # the fence: automation may not act now
                s.perform(ActionType.press, None, "Enter")
            seen.append(control.owner)
            s.page.frame(name="body").click("input[value='I Acknowledge']")     # a person, in the same session
        control.operator = ScriptedOperator(surface, human, control, resume="next_step", note="acknowledged the notice")

    res = run(env, cap, on_stuck="escalate", surface_hook=hook)
    assert res.status == "success" and res.outputs["available_balance"] == "4721.37"
    h = res.handoffs[0]
    assert h.reason == "unexpected_state" and h.step_id == "s3" and h.resolution == "next_step" and h.human_actions >= 1
    assert seen == ["human"]
    ev = events(env, res)
    owners = [(e["data"]["from_owner"], e["data"]["to_owner"]) for e in ev if e["type"] == "control_transfer"]
    assert owners == [("automation", "paused"), ("paused", "human"), ("human", "automation")]
    assert any(e["type"] == "human_actions" and e["actor"] == "human" for e in ev)
    ticket_file = next((env.out / res.run_id).glob("intervention-*.json"))
    req = json.loads(ticket_file.read_text(encoding="utf-8"))
    assert req["reason"] == "unexpected_state" and req["step_id"] == "s3" and req["evidence"]["screenshot"]


def test_human_can_abort_and_the_run_ends_escalated(env):
    env.srv.set_faults(notice=True)
    cap = fast(env.cap)
    cap.recoverables = []

    def hook(surface, control):
        control.operator = ScriptedOperator(surface, lambda s: None, control, resume="abort", note="cannot help")

    res = run(env, cap, on_stuck="escalate", surface_hook=hook)
    assert res.status == "escalated" and res.handoffs[0].resolution == "abort"


def test_stuck_without_escalation_fails_with_evidence(env):
    env.srv.set_faults(notice=True)
    cap = fast(env.cap)
    cap.recoverables = []
    res = run(env, cap)                                       # on_stuck defaults to 'fail'
    assert res.status == "failed" and res.failure.category.value == "checkpoint_failed" and res.failure.evidence


def test_approval_can_come_from_a_human_through_the_same_handoff(env):
    cap = with_commit(env.cap2)

    def approve(resume):
        def hook(surface, control):
            control.operator = ScriptedOperator(surface, lambda s: None, control, resume=resume)
        return hook

    denied = run_open(env, cap, surface_hook=approve("abort"))
    assert denied.status == "blocked" and env.srv.commits == [] and denied.handoffs[0].reason == "needs_approval"
    env.srv.reset()
    ok = run_open(env, cap, surface_hook=approve("retry_step"))      # 'retry the step' == approve and execute
    assert ok.status == "success" and len(env.srv.commits) == 1 and ok.handoffs[0].resolution == "retry_step"
