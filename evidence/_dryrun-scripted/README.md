# Evidence index (scripted discovery)

All runs use the bundled mock console with synthetic data. Logs are redacted at the write boundary; `caller view` below is what the calling agent received (the persisted `result.json` masks financial outputs).

* Discovery: `6` steps, stop reason `finished`, tokens {'in': 0, 'out': 0}; second capability `9` steps.
* Verification replays (fresh browser, no LLM): verify-20260919-105532-091, verify-20260919-105540-987
* Artifacts: `artifact/*.json` (also in `/capabilities`).

| scenario | status | outcome / failure | recovered | drift | handoff | app-side commits | caller view (outputs) | run dir |
|---|---|---|---|---|---|---|---|---|
| **success** — happy path, screenshot after each step | `success` |  | - | - | - | 0 | {"available_balance": "4721.37"} | `replay-success-20260919-105545-127` |
| **invalid-input** — rejected up front; the app is never touched | `failed` | invalid_input @ None: 'member_id' does not match the required format | - | - | - | 0 | - | `replay-invalid-input-20260919-105548-626` |
| **business-not-found** — expected business outcome, NOT an error | `business_outcome` | record_not_found | - | - | - | 0 | - | `replay-business-not-found-20260919-105549-620` |
| **business-restricted** — permission denial declared as an outcome | `business_outcome` | access_restricted | - | - | - | 0 | - | `replay-business-restricted-20260919-105552-472` |
| **business-no-savings** — control absent = business answer (on_target_missing) | `business_outcome` | no_savings_account | - | - | - | 0 | - | `replay-business-no-savings-20260919-105555-330` |
| **recovered-notice** — unexpected compliance dialog: acknowledged and continued | `success` |  | compliance_notice x1 | - | - | 0 | {"available_balance": "4721.37"} | `replay-recovered-notice-20260919-105559-861` |
| **recovered-transient-503** — two 503s: backoff + reload | `success` |  | transient_unavailable x2 | - | - | 0 | {"available_balance": "4721.37"} | `replay-recovered-transient-503-20260919-105603-461` |
| **recovered-session-timeout** — session dies mid-run: re-authenticate, restart from step 1 | `success` |  | session_expired x1 | - | - | 0 | {"available_balance": "4721.37"} | `replay-recovered-session-timeout-20260919-105608-915` |
| **slow-pages** — condition-based waiting, no fixed sleeps | `success` |  | - | - | - | 0 | {"available_balance": "4721.37"} | `replay-slow-pages-20260919-105613-419` |
| **HARD-FAILURE-app-error** — raw ORA- error: stop, structured failure + screenshot + DOM snapshot | `failed` | app_error @ s4: application_error: Raw application error page (HTTP 500 / unhandled exception). | - | - | - | 0 | - | `replay-HARD-FAILURE-app-error-20260919-105620-672` |
| **HARD-FAILURE-transient-exhausted** — retry budget exhausted -> hard failure, retriable=true | `failed` | app_error @ s3: recoverable 'transient_unavailable' persisted after 3 attempt(s) | transient_unavailable x3 | - | - | 0 | - | `replay-HARD-FAILURE-transient-exhausted-20260919-105624-085` |
| **open-subaccount-to-review** — stops at the confirmation screen; commits nothing | `success` |  | - | - | - | 0 | - | `replay-open-subaccount-to-review-20260919-105631-095` |
| **open-subaccount-validation** — app-side validation -> input_rejected outcome | `business_outcome` | input_rejected | - | - | - | 0 | - | `replay-open-subaccount-validation-20260919-105635-583` |
| **BLOCKED-irreversible-understated** — artifact claims the commit is 'safe'; runtime re-derives irreversible and refuses | `blocked` | policy_violation @ s99: irreversible step requires approval and none was given | - | - | - | 0 | - | `replay-BLOCKED-irreversible-understated-20260919-105640-270` |
| **approval-DENIED-by-human** — irreversible step escalated; operator denies | `blocked` | policy_violation @ s99: irreversible step requires approval and none was given | - | - | needs_approval->abort (0 human actions) | 0 | - | `replay-approval-DENIED-by-human-20260919-105644-680` |
| **approval-GRANTED-by-human** — operator approves; automation performs the single commit | `success` |  | - | - | needs_approval->retry_step (0 human actions) | 1 | - | `replay-approval-GRANTED-by-human-20260919-105649-808` |
| **HANDOFF-human-takeover** — unknown state -> ticket -> human drives the SAME session -> hand back -> resume | `success` |  | - | - | unexpected_state->next_step (3 human actions) | 0 | {"available_balance": "4721.37"} | `replay-HANDOFF-human-takeover-20260919-105654-693` |
| **tenant-lakeside-NO-override** — different wording: fails with a precise, debuggable error | `failed` | target_not_found @ s3: could not resolve control: link 'Search' | - | - | - | 0 | - | `replay-tenant-lakeside-NO-override-20260919-105700-038` |
| **tenant-lakeside-WITH-override** — same base artifact + small additive override | `success` |  | - | s3:locator_fallback | - | 0 | {"available_balance": "4721.37"} | `replay-tenant-lakeside-WITH-override-20260919-105703-964` |
