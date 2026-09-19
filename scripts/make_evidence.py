"""Regenerate /evidence: one discovery run (real LLM by default) + replays under every runtime condition.

    python scripts/make_evidence.py --provider anthropic          # the real thing (needs ANTHROPIC_API_KEY)
    python scripts/make_evidence.py --provider scripted           # key-free dry run -> evidence/_dryrun-scripted

Everything runs against the bundled mock console with synthetic data.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from hands import scripts  # noqa: E402
from hands.envfile import load_env  # noqa: E402
from hands.control import ScriptedOperator  # noqa: E402
from hands.harness import MockServer, with_commit_step  # noqa: E402
from hands.pipeline import discover, replay  # noqa: E402
from hands.policy import load_policy  # noqa: E402
from hands.recorder import load_capability, save_capability  # noqa: E402
from hands.schema import Locator  # noqa: E402
from hands.tenancy import StepPatch, TenantOverride  # noqa: E402

POLICY = load_policy(ROOT / "policies" / "msc.policy.json")
PROFILE = ROOT / "profiles" / "msc.json"


def models(provider: str, model: str | None):
    if provider == "scripted":
        return scripts.savings_balance_script(), scripts.open_review_script(), "scripted"
    if provider == "openai":
        from hands.llm import OpenAIClient
        return OpenAIClient(model), OpenAIClient(model), "openai"
    from hands.llm import AnthropicClient
    return AnthropicClient(model), AnthropicClient(model), "anthropic"


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="anthropic", choices=["anthropic", "openai", "scripted"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    out = Path(args.out or (ROOT / "evidence" / ("_dryrun-scripted" if args.provider == "scripted" else "")))
    caps = out / "capabilities"
    out.mkdir(parents=True, exist_ok=True)
    def rm(path: Path) -> None:
        for attempt in range(6):         # OneDrive / antivirus can hold a folder for a moment on Windows
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                return
            except PermissionError:
                if sys.platform == "win32":      # OneDrive marks folders read-only / reparse points; PowerShell copes
                    subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Remove-Item -LiteralPath '" + str(path).replace("'", "''") + "' -Recurse -Force -ErrorAction SilentlyContinue"],
                                   capture_output=True)
                    if not path.exists():
                        return
                time.sleep(1.5)
        raise SystemExit(f"cannot clean {path}: still locked (pause OneDrive sync or move the project out of OneDrive)")

    for child in out.iterdir():          # regenerate only this provider's evidence; keep '_'-prefixed sets (dry runs)
        if not child.name.startswith("_"):
            rm(child)                    # fail loudly rather than mix old and new runs
    m1, m2, label = models(args.provider, args.model)
    rows: list[dict] = []
    head = not args.headed

    with MockServer(port=8765) as prairie, MockServer(tenant="lakeside", port=8766) as lakeside:
        print("== 1. DISCOVERY (LLM in the loop) ==")
        cap, d1, v1 = discover(target=prairie.url, goal=scripts.SAVINGS_BALANCE_GOAL, model=m1, policy=POLICY,
                               profile_path=PROFILE, out_dir=out, cap_dir=caps, headless=head, tenant="prairie",
                               product_version="MSC 7.2.1", max_steps=15, token_budget=60000,
                               cap_id="member_savings_balance")
        if cap is None:
            sys.exit("discovery did not produce a capability; see the run log under " + str(out))
        print("== 1b. second capability: reach the review screen, do NOT submit ==")
        cap2, d2, v2 = discover(target=prairie.url, goal=scripts.OPEN_REVIEW_GOAL, model=m2, policy=POLICY,
                                profile_path=PROFILE, out_dir=out, cap_dir=caps, headless=head, tenant="prairie",
                                product_version="MSC 7.2.1", max_steps=15, token_budget=60000,
                                cap_id="open_subaccount_to_review")
        assert not prairie.commits, "discovery must never have committed anything"

        def go(name, srv, capability, inputs, note, commits=0, **kw):
            print(f"== replay: {name} ==")
            res = replay(capability, inputs, target=srv.url, policy=POLICY, out_dir=out, headless=head,
                         label=f"replay-{name}", **kw)
            done = len(srv.commits)            # irreversible actions the APP actually executed, read before reset
            assert done == commits, f"{name}: app executed {done} commit(s), expected {commits}"
            rows.append({"scenario": name, "note": note, "run": res.run_id, "status": res.status, "commits": done,
                         "outcome": res.outcome, "outputs": res.outputs,
                         "recoveries": [f"{r.code} x{r.attempts}" for r in res.recoveries],
                         "drift": [f"{x.step_id}:{x.kind}" for x in res.drift],
                         "handoffs": [f"{h.reason}->{h.resolution} ({h.human_actions} human actions)" for h in res.handoffs],
                         "failure": (f"{res.failure.category.value} @ {res.failure.step_id}: {res.failure.message}"
                                     if res.failure else None)})
            prairie.reset()
            lakeside.reset()
            return res

        # Inputs come from the artifact itself: the real model chooses its own parameter names.
        mp = cap.inputs[0].name
        mid = {mp: d1.examples[mp]}
        go("success", prairie, cap, mid, "happy path, screenshot after each step", shots="steps")
        go("invalid-input", prairie, cap, {mp: "12ab"}, "rejected up front; the app is never touched")
        go("business-not-found", prairie, cap, {mp: "99999"}, "expected business outcome, NOT an error")
        go("business-restricted", prairie, cap, {mp: "77777"}, "permission denial declared as an outcome")
        go("business-no-savings", prairie, cap, {mp: "40551"}, "control absent = business answer (on_target_missing)")
        prairie.set_faults(notice=True)
        go("recovered-notice", prairie, cap, mid, "unexpected compliance dialog: acknowledged and continued")
        prairie.set_faults(flaky_member=2)
        go("recovered-transient-503", prairie, cap, mid, "two 503s: backoff + reload")
        prairie.set_faults(expire_after=2)
        go("recovered-session-timeout", prairie, cap, mid, "session dies mid-run: re-authenticate, restart from step 1")
        prairie.set_faults(slow_ms=2000)
        go("slow-pages", prairie, cap, mid, "condition-based waiting, no fixed sleeps")
        prairie.set_faults(app_error_acct=True)
        go("HARD-FAILURE-app-error", prairie, cap, mid, "raw ORA- error: stop, structured failure + screenshot + DOM snapshot")
        prairie.set_faults(flaky_member=50)
        go("HARD-FAILURE-transient-exhausted", prairie, cap, mid, "retry budget exhausted -> hard failure, retriable=true")

        oi = {p.name: (d2.examples[p.name] == "true" if p.type.value == "boolean" else d2.examples[p.name])
              for p in cap2.inputs}
        dep = next(p.name for p in cap2.inputs if "deposit" in p.name)
        go("open-subaccount-to-review", prairie, cap2, oi, "stops at the confirmation screen; commits nothing")
        go("open-subaccount-validation", prairie, cap2, {**oi, dep: "2.00"}, "app-side validation -> input_rejected outcome")
        risky = with_commit_step(cap2, declared_risk="safe")
        go("BLOCKED-irreversible-understated", prairie, risky, oi, "artifact claims the commit is 'safe'; runtime re-derives irreversible and refuses")

        def approver(resume):
            def hook(surface, control):
                control.operator = ScriptedOperator(surface, lambda s: None, control, resume=resume)
            return hook
        go("approval-DENIED-by-human", prairie, risky, oi, "irreversible step escalated; operator denies", surface_hook=approver("abort"))
        go("approval-GRANTED-by-human", prairie, risky, oi, "operator approves; automation performs the single commit",
           commits=1, surface_hook=approver("retry_step"))

        # human takeover of the live session (an UNKNOWN interstitial the artifact has no handler for)
        stuck = cap.model_copy(deep=True)
        stuck.recoverables = []
        for s in stuck.steps:
            s.timeout_ms = 1500
        prairie.set_faults(notice=True)

        def takeover(surface, control):
            def human(s):
                s.page.frame(name="body").click("input[value='I Acknowledge']")
            control.operator = ScriptedOperator(surface, human, control, resume="next_step", note="acknowledged the notice")
        go("HANDOFF-human-takeover", prairie, stuck, mid, "unknown state -> ticket -> human drives the SAME session -> hand back -> resume",
           on_stuck="escalate", surface_hook=takeover)

        # multi-tenant reuse: base artifact recorded on 'prairie', replayed on 'lakeside'
        go("tenant-lakeside-NO-override", lakeside, cap, mid, "different wording: fails with a precise, debuggable error")
        ov = TenantOverride(
            tenant="lakeside", capability_id=cap.id, base_version=cap.version, base_digest=cap.digest,
            notes="Lakeside labels the search link 'Find' and the balance 'Avail Balance'. Additive only.",
            step_patches={"s3": StepPatch(add_locators=[Locator(kind="role_name", role="link", name="Find", stability="medium",
                                                                note="lakeside wording")]),
                          "s5": StepPatch(add_extraction_labels={"available_balance": ["Avail Balance"]})})
        ov_path = ROOT / "overrides" / f"lakeside.{cap.id}.json"
        ov_path.parent.mkdir(exist_ok=True)
        ov_path.write_text(ov.model_dump_json(indent=2), encoding="utf-8")
        go("tenant-lakeside-WITH-override", lakeside, cap, mid, "same base artifact + small additive override", override=ov)

    # -------------------------------------------------------------------- write-up
    if args.provider != "scripted":       # the README demo commands read /capabilities
        live = ROOT / "capabilities"
        for old in live.glob("*.json"):
            old.unlink()
        for c in (cap, cap2):
            save_capability(load_capability(caps / f"{c.id}.json"), live / f"{c.id}.json")
    (out / "artifact").mkdir(exist_ok=True)
    for c in (cap, cap2):
        save_capability(load_capability(caps / f"{c.id}.json"), out / "artifact" / f"{c.id}.json")
    lines = [f"# Evidence index ({label} discovery)\n",
             "All runs use the bundled mock console with synthetic data. Logs are redacted at the write boundary; "
             "`caller view` below is what the calling agent received (the persisted `result.json` masks financial outputs).\n",
             f"* Discovery: `{d1.steps_used}` steps, stop reason `{d1.stop_reason}`, tokens {d1.usage}; second capability `{d2.steps_used}` steps.",
             f"* Verification replays (fresh browser, no LLM): {v1.run_id if v1 else '-'}, {v2.run_id if v2 else '-'}",
             "* Artifacts: `artifact/*.json` (also in `/capabilities`).\n",
             "| scenario | status | outcome / failure | recovered | drift | handoff | app-side commits | caller view (outputs) | run dir |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        res = r["outcome"] or r["failure"] or ""
        lines.append(f"| **{r['scenario']}** — {r['note']} | `{r['status']}` | {res} | {', '.join(r['recoveries']) or '-'} | "
                     f"{', '.join(r['drift']) or '-'} | {', '.join(r['handoffs']) or '-'} | {r['commits']} | "
                     f"{json.dumps(r['outputs']) if r['outputs'] else '-'} | `{r['run']}` |")
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nEvidence written to {out}")


if __name__ == "__main__":
    main()
