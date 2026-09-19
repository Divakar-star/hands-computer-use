# Design write-up

**Through-line:** the model discovers → the artifact becomes a capability → deterministic replay is how an agent invokes it.
The target is a mock legacy console (`mockbank/`: framesets, nested tables, no ids/test-ids, `javascript:` links, errors as HTTP 200)
with injectable faults and two tenants. Evidence for every claim below is indexed in [`evidence/README.md`](evidence/README.md); diagrams are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## 1. Architecture

```
Surface (Playwright)  ──observe──►  Observation{frames, Element descriptors}  ◄── the seam
   ▲ perform                          │                     │
   │                        DiscoveryAgent (LLM)     ReplayEngine (no LLM)
   │                          │  trace of descriptors        ▲  Capability JSON
   │                          └────►  Recorder  ─────────────┘
   └── Policy (allowlist · risk) · Redactor · RunLog(evidence) · ControlPlane (human handoff)
```

Single process, synchronous, no queue — the brief rewards a small correct core over scaling plumbing. Key decisions:

* **Perception is semantic, not selector-based.** Every control becomes a descriptor: role, accessible name (including the *label in the adjacent
  table cell*, how legacy apps label inputs), form-field `name`, href / form action, ordinal, frame. The model sees a compact text rendering
  (frames, visible text, label/value pairs, numbered controls); screenshots are optional (`--vision`). *Trade-off:* text is cheap and
  deterministic but presumes a DOM/AX tree. A screenshot-and-coordinates surface plugs in behind the same `Observation` type (§4).
* **The artifact stores descriptors, never raw selectors or transcripts,** so recording, replay and tests all share one matching function
  (`matching.resolve`, pure Python, unit-tested without a browser). The recorder can therefore *prove* each locator resolves to exactly the recorded control.
* **Stateless model turns.** Each step: goal + a compact history of its own actions + the current (redacted) screen; exactly one tool call with a
  rationale (`tool_choice=any`). Cheaper and more robust than a growing chat; the log records *what and why* per step. Provider is a 1-method interface
  (Anthropic and OpenAI adapters are written; the OpenAI one is what the evidence was produced with, the Anthropic one is tested only against stubs).
* **The real discovery run** (`evidence/discovery-*`, model `gpt-5.6-luna` via Chat Completions, reasoning effort `none`): capability 1 (read a savings balance) finished in **6 model calls,
  9.5k input / 0.4k output tokens (~$0.002)**; capability 2 (reach the review screen, never submit) in **9 calls, 15.4k / 1.3k (~$0.005)**. Both compiled into artifacts and were then
  verified by replay in a fresh browser with no model. Token spend is bounded (`--max-steps 15`, `--token-budget 60000`, a per-call output cap). Three things the live model taught me, each fixed and tested:
  (1) the API rejects function tools combined with a reasoning effort on this model, so the adapter defaults to `none`; (2) with the original prompt the model did not declare that "click Share Savings"
  can legitimately be absent, so a member with no savings account would have been a hard failure — the instructions now ask for this on every list-pick and it is declared; (3) the model baked the
  *member-consent checkbox* into the artifact as a constant, and the recorder's value-matching also bound an unrelated radio to the consent parameter. See §6 (attestations) and the regression tests.
* **Credentials are outside the capability.** A `SessionProvider` signs in; the model never sees a password and artifacts never contain one. Session
  expiry is a bounded *recovery* (re-auth + restart), not a replayed login flow.
* **A recording is trusted only after it replays.** The compiled artifact is replayed in a fresh browser with the discovery inputs and must reproduce
  the values the model read → `draft → verified`; a human moves it to `approved`.
* **One browser thread.** Playwright's sync API is thread-bound, so during a handoff the automation thread stays the sole owner of the browser and
  services operator requests (§5). (Found the hard way: the first version called Playwright from the operator's thread and failed.)

## 2. Artifact schema

`hands/schema.py` (JSON Schema exported by `python -m hands schema`). A `Capability` carries: identity (`id`, semver `version`, `status`), `app`
(vendor product + version seen, *not* a tenant), `entry`, typed `inputs`/`outputs`, ordered `steps`, a final `success` condition, and three
**separate** lists — `outcomes`, `recoverables`, `failure_signatures` — plus `review` (max risk, irreversible steps), `provenance` and a content `digest`.

* **`Target.locators` is an ordered list of independent strategies** (`field_name`, `href`, `role_name`, `visible_text`, `structural`), each with a
  stability tag and a note. The recorder keeps a strategy only if it is unique on the recorded screen, drops any containing sensitive text, and stores its
  ordering rationale. Machine identifiers (field name, templated href) rank above human wording because they survive relabelling across tenants;
  positional locators are kept only if nothing sturdier is unique.
* **`Value = param | literal`**; concrete data is replaced by `{{param}}` in typed values, href locators and URL checkpoints, so the artifact contains no member data.
  Dropdowns are recorded by option *value* (`SAV`), not tenant wording ("Share Savings"), and become a typed `enum` input.
* **One `Condition` vocabulary** (`text_contains/regex`, `frame_url`, `status_in`, `element_present`, `all/any/not`) is reused for step checkpoints,
  outcomes, recoverables and failure signatures — small surface, easy to review.
* **Outputs and inputs carry `sensitivity`** (`none|identifier|financial|pii|secret`); it drives log masking, and sensitive params cannot carry an `example`.
* **Risk is a reviewable claim, not the enforcement** (§6). **The transcript is not embedded** — only a reference to the redacted run log.
* **Agent-facing view:** `Capability.to_tool_spec()` yields a function-calling tool + result contract (`python -m hands catalog`).

## 3. Determinism & error handling

*Determinism:* controls resolve from descriptors via the ordered cascade; if a strategy matches ≠1 element the next is tried, and **ambiguity is never guessed**
(hard failure `target_ambiguous`). Waits are condition-based (`settle()`: no requests in flight and every frame loaded, stable 200 ms) — no fixed sleeps.
Every step has a **postcondition** derived at record time from what changed (which frame's URL) that must hold before continuing; a final `success` checkpoint
closes the run. A checkpoint is never satisfied while any frame reports HTTP ≥ 400 (a test caught an error page at the expected URL passing the check).

*When a postcondition is not met* the engine classifies the screen, in order, using lists carried by the artifact:

| Class | Example | Response | Result |
|---|---|---|---|
| **Recoverable** | compliance interstitial · 503 · session timeout | bounded handler (click / backoff+reload / re-auth+restart from step 1, only if no irreversible step ran) | `success` + `recoveries[]` |
| **Business outcome** | "no member record", restricted record, member has no savings account, app-side validation | return it; never an exception | `business_outcome` + code + guidance |
| **Hard failure** | raw `ORA-` error, retries exhausted, control not found/ambiguous, checkpoint unmet | stop; `failure{category, step_id, expected, observed, retriable}` + masked screenshot + DOM snapshot | `failed` |
| **Blocked / escalated** | irreversible step without approval; unknown state with a human available | §5, §6 | `blocked` / `escalated` |

Bad caller input is rejected *before the app is touched* (`invalid_input`). "Control absent" is a first-class business answer (`on_target_missing`),
which is how "member has no savings account" avoids being a `target_not_found`.
*Drift* (secondary): a fallback locator used, two strategies disagreeing, or a control found in another frame is reported in `drift[]` while the flow still runs.
Recorded on one tenant and replayed on another, the base artifact fails with a precise `target_not_found @ s3`; with a tiny override it passes and reports `s3:locator_fallback`.

## 4. Heterogeneity & multi-tenant

**Surfaces.** `Observation`/`Element` are technology-neutral: `frame` = window/pane, `role`+`name` come from the accessibility node, `name_attr` from the
automation id, `href` is absent on desktop. A **legacy web** surface is what is implemented (frames, adjacent-cell labels, `javascript:` links, non-semantic
markup). A **desktop** surface would fill the same shapes from UIA/AX and reuse `matching`, `replay`, `policy`, `redact`, `control` unchanged; where no tree exists,
elements become OCR/vision regions (`role=region`, `name`=text) with a coordinate action — a new locator kind (`visual_text`), not a new artifact format. Steps say
"click *this control*", never "click (x,y) / this selector".

**Multi-tenant.** Key artifacts by *product* (+ version range), not tenant. A tenant supplies an **override** (`tenancy.py`) that can only **add** fallback locators
and label aliases — never remove a locator, change an action/risk, or widen policy — and it **pins the base artifact's digest**, so re-recording the base marks
overrides stale instead of silently mis-applying them. Product-wide knowledge (interstitials, error signatures) lives in a per-product *profile* copied into each artifact
so artifacts stay self-contained. Drift management at scale: every replay already emits `locator_fallback`/`strategy_disagreement`; aggregate by (product, version, tenant),
canary-replay read-only capabilities per tenant on a schedule, and when a fallback fires repeatedly propose an override from a *single-step* assisted rediscovery (not built).
Prefer machine identifiers over wording; put tenant wording in overrides.

## 5. Escalation & handoff

**Detecting "stuck".** *Replay:* an unrecognised screen after the checkpoint window (no recoverable/outcome/signature matches); an irreversible step lacking approval.
*Discovery:* model calls `ask_human`; the same screen+action four times (no progress); four consecutive failed/blocked actions; an irreversible action wanted.
A ticket carries the capability/goal, step, reason, expected-vs-observed, redacted page excerpt and a *masked* screenshot + DOM snapshot (`intervention-<id>.json`).

**Control model** (`control.py`): `owner ∈ {automation, paused, human}`. Escalation flips `automation → paused` and **fences the session** — `Surface.perform/goto` raise
`ControlViolation` (enforced at the surface, not by convention). An operator *claims* the ticket (`paused → human`) and drives **the same live browser** (same cookies, page,
position in the flow) — the headed window is the co-browsing surface. Human clicks/changes/submits are captured as *kind + target + value length* (values never recorded)
and written to the log and ticket. On hand-back the operator chooses `next_step` (I did it), `retry_step`, or `abort`; the engine **re-verifies the checkpoint before trusting a
human** (`next_step`) and re-runs the step on `retry_step`. Every transition (`automation→paused→human→automation`) is a log event. For approvals, `retry_step` = approve.
**Mocked:** the operator UI (a bare Flask page, a terminal prompt, and a scripted stand-in used in tests). **Real:** the ticket, the fence, same-session takeover, action
capture, resume/verify. **Not built:** auth, queues/SLAs, multi-operator claim races, remote co-browsing for headless runs.

## 6. Safety

* **Allowlist, enforced three ways:** explicit origin + route globs + allowed action types (`policies/msc.policy.json`); a **network-level route guard** aborting any request
  outside it (so a bug or a page redirect cannot leave the app); and denied routes (`/__admin*`, `/msc/logoff*`).
* **Risk classes** — `safe` run · `reversible` (form entry) run · `irreversible` (commit) **blocked unless explicitly approved**, else escalated · `forbidden` (sign-off, password entry,
  disallowed actions) never. Blocking irreversible steps by default is the conservative choice: a false positive costs one human click, a false negative posts to a core system.
  **Risk is re-derived at runtime from the live control** (its label, href, form action), so an artifact that *understates* a step's risk is still stopped (tested: a step declared `safe`
  that clicks "Confirm and Submit" → `blocked`, and the app's own commit counter stays 0). Unknown apps fall back to a fail-safe name heuristic.
* **Data handling:** (1) sensitive *labelled* fields (SSN, DOB, phone, address, name/holder…) are masked in what the model sees and extraction of them is refused;
  (2) values are learned when first seen and scrubbed everywhere later (`Dana Whitfield` in a heading → `[NAME]`), also from the `id - Full Name` display format;
  (3) registered secrets/identifiers are scrubbed verbatim; (4) redaction happens at the *write boundary* of the logger, so a forgotten call site can't leak;
  (5) screenshots are masked **in the page before capture**; (6) financial outputs are returned to the caller but masked in persisted logs; (7) the recorder **refuses to save** an artifact
  containing a registered value or SSN-shaped string. Passwords are never read into an observation.
* **Attestations are never automated on the caller's behalf.** A consent / disclosure / certification checkbox is a person's statement. The recorder turns any such ticked box into a **required
  boolean input** (`member_consent_and_disclosures_provided`) even if the model declared nothing, and binds checkboxes to parameters by label relevance, never by the value "true" — otherwise a
  recorded capability would silently attest consent on every future call.
* **Limits (stated plainly).** Redaction is pattern + learned-value based, so PII in a position no rule anticipates can pass. *This happened:* scanning the evidence found a member's name in an
  unlabeled cell on the review screen (`12345 - Dana Whitfield`) in a DOM snapshot; I fixed the format/labels and added a regression test, but the class remains — production needs
  per-product field schemas or NER, and persisting page text only on failure. Non-sensitive page text (including balances) still goes to the model provider (needs zero-retention terms).
  The route guard protects the browser only; policy/profile are unsigned repo config; the artifact digest detects post-save edits but is not a signature; the operator page has no auth.

## 7. Cuts

**Deliberately left out:** desktop/vision surface (designed, not built); a real operator console / remote co-browsing; a multi-tenant registry, fleet drift telemetry and scheduled canaries;
assisted single-step LLM recovery; multi-run stability scoring; an approval workflow beyond `draft→verified` (status is recorded, unattended replay is not yet gated on `approved`);
tenant overrides for *text-based* success checks (the recorder prefers URL + extraction evidence so this rarely matters); Anthropic adapter is untested against the live API; reasoning-enabled OpenAI runs would need the Responses API (Chat Completions rejects tools + reasoning on this model).
**Known shortfall:** business outcomes come from the product profile plus steps the model declares conditional — the agent does not yet *probe* bad inputs during discovery to learn them, and
whether the model declares conditional steps depends on the prompt (it missed it once, until the instructions were sharpened); a probe pass (run discovery with a not-found / restricted member)
is the first thing I would add. Only two capabilities were recorded, on one model, so I make no claim about robustness across models or apps. **Next:** (1) probe pass + confidence score from N-run stability; (2) gate unattended replay on `approved`; (3) tenant registry with override proposals from single-step rediscovery;
(4) UIA/AX desktop surface + `visual_text` locator; (5) production operator console; (6) per-product PII schemas replacing heuristics.
