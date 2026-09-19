# hands — computer-use capabilities for UIs that have no API

An LLM works out how to complete a task inside a legacy back-office web app **once**. The successful
run is compiled into a typed, versioned **capability artifact**. That artifact then **replays
deterministically with no model in the loop**, returns typed outputs, distinguishes business outcomes
from recoverable conditions from hard failures, refuses risky actions, and can hand the *live session*
to a human and take it back.

```
 goal ──► DISCOVERY (LLM: observe → decide → act) ──► RECORDER ──► capability.json ──► REPLAY (no LLM)
                          │                                                    │
                          └──── guardrails (allowlist · risk · redaction) ─────┴──► human handoff
```

Design write-up: [`REPORT.md`](REPORT.md). Diagrams: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Evidence of real runs: [`evidence/`](evidence/).

## What is in the box

| Path | What |
|---|---|
| `mockbank/` | The **target**: a mock legacy core-banking console (framesets, nested tables, no ids/test-ids, `javascript:` links, business errors as HTTP 200) with switchable fault injection and two "tenants". Synthetic data only. |
| `hands/` | The system: `schema` (artifact + result contract), `surface` (Playwright, frames, semantic descriptors), `agent` + `llm` (discovery loop), `recorder`, `replay`, `policy`, `redact`, `control` (handoff), `tenancy`, `evidence`, `cli`. |
| `profiles/msc.json`, `policies/msc.policy.json` | Product knowledge (known interstitials / error signatures) and the guardrail policy (allowlist, risk rules). |
| `capabilities/` | Saved artifacts. `overrides/` holds per-tenant overrides. |
| `evidence/` | Logs, screenshots and artifacts from the discovery and replay runs. |
| `tests/` | 88+ tests; the scenario suite drives a real browser. |

## Setup

Python 3.11+ (developed on 3.14).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

**Keys / config.** Discovery needs one model provider. Replay needs **no key at all**.

```bash
cp .env.example .env                # then edit .env: set OPENAI_API_KEY=... or ANTHROPIC_API_KEY=...
                                    # (.env is git-ignored and loaded automatically; real env vars win)
# optional model override, in .env or the shell:
#   HANDS_MODEL=...                 # defaults: claude-sonnet-5 (--provider anthropic), gpt-5.6-luna (--provider openai)
```

The mock console's login is `teller01` / `demo-not-a-real-password` (override with `MSC_USER` / `MSC_PASS`).

### Running without any live service or key
* `--provider scripted-savings` / `scripted-open` runs discovery with a deterministic stand-in "model"
  (a **dry run — not** the real LLM discovery run; it is labelled as such everywhere).
* `--spawn-mock` starts the bundled console in-process, so no second terminal is needed.
* `pytest` needs neither a key nor a running server.

## Demo path

```bash
# 1) DISCOVER: a real LLM drives the mock console, the run is recorded, compiled and verified by replay
python -m hands discover --spawn-mock \
  --goal "Look up member 12345 in the member servicing console and read their savings account's available balance." \
  --provider openai            # or: --provider anthropic

# 2) REPLAY the saved artifact: no LLM, typed inputs, structured result
python -m hands replay capabilities/msc.member_savings_balance.json --spawn-mock --input member_id=12345 --shots

# 3) REPLAY under runtime conditions (each prints a structured result; exit code 0 = success/business outcome)
python -m hands replay capabilities/msc.member_savings_balance.json --spawn-mock --input member_id=99999            # business outcome: record_not_found
python -m hands replay capabilities/msc.member_savings_balance.json --spawn-mock --input member_id=12345 --mock-faults notice           # recovered
python -m hands replay capabilities/msc.member_savings_balance.json --spawn-mock --input member_id=12345 --mock-faults app_error_acct    # hard failure + evidence

# 4) What an AI agent sees: saved capabilities as callable tool specs
python -m hands catalog
```

`--spawn-mock` runs the console just for that command. To use your own terminal instead:
`python -m mockbank --port 8765 [--tenant lakeside] [--faults notice,slow_ms=2500]`.

Regenerate the whole evidence suite (discovery + 19 replay scenarios + summary table):

```bash
python scripts/make_evidence.py --provider openai        # real discovery run  -> evidence/  (or anthropic)
python scripts/make_evidence.py --provider scripted      # key-free dry run    -> evidence/_dryrun-scripted/
```

### Human handoff, live
```bash
python scripts/demo_handoff.py          # opens a visible browser + operator page at http://127.0.0.1:8770
python scripts/demo_handoff.py --cli    # terminal operator instead
```
It replays the open-sub-account capability with a final irreversible "Confirm and Submit" step. Automation
stops at the review screen and raises an approval request; you take control of the same live browser window,
then hand back (*approve* / *I did it* / *abort*). Irreversible steps always ask (`needs_approval`); unknown
screens ask when `--escalate {cli,http}` is passed to `replay`/`discover`.

## Tests

```bash
pytest -q                       # everything (~2.5 min: the scenario suite launches real browsers)
pytest tests/test_core.py -q    # pure logic, <1s: schema, locator resolution, policy, redaction, tenancy
```

## Result contract (what a calling agent gets back)

`status` is one of `success | business_outcome | blocked | escalated | failed`, plus typed `outputs`,
`outcome` (a business-outcome code such as `record_not_found`), `recoveries`, `drift`, `handoffs`, and on
failure a `failure {category, step_id, expected, observed, retriable, evidence{screenshot,dom_snapshot}}`.
The JSON Schema for both the artifact and the result is exported by `python -m hands schema`.
