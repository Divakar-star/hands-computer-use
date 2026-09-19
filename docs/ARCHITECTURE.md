# Architecture

Five diagrams (GitHub renders the Mermaid below). Prose and trade-offs are in [`../REPORT.md`](../REPORT.md).
Dashed boxes are **designed, not built**.

## 1. Everything above the seam is surface-agnostic

Only a `Surface` knows how a control was found. The recorded flow says "click *this control*", never a selector or coordinate,
so a desktop or vision surface reuses the recorder, replay engine, policy, redaction and handoff unchanged.

```mermaid
flowchart TB
  subgraph apps["Applications"]
    A1["Legacy web app<br/>framesets, tables, no test ids"]
    A2["Desktop app<br/>accessibility tree"]:::planned
    A3["No usable tree<br/>screenshot + OCR"]:::planned
  end
  subgraph surfaces["Surfaces"]
    S1["PlaywrightSurface<br/>descriptors across all frames<br/>masked screenshots, route guard"]:::det
    S2["UIA / AX surface"]:::planned
    S3["Vision surface"]:::planned
  end
  OBS["<b>Observation</b> = frames + Element descriptors<br/>role, accessible name, group label, field name, href, form action, ordinal<br/>(the seam: nothing above knows about HTML)"]
  AG["DiscoveryAgent<br/>LLM, one tool call per step"]:::model
  RC["Recorder<br/>trace to Capability<br/>every locator proven unique"]:::det
  RP["ReplayEngine<br/>no LLM, condition-based waits"]:::det
  subgraph shared["Applied to both paths"]
    P["Policy<br/>allowlist, risk classes"]:::guard
    R["Redactor<br/>at the write boundary"]:::guard
    L["RunLog + evidence"]:::guard
    C["ControlPlane<br/>automation / paused / human"]:::guard
  end
  A1 -->|"DOM, every frame"| S1
  A2 -.-> S2
  A3 -.-> S3
  S1 -->|observe| OBS
  OBS -->|perform| S1
  S2 -.-> OBS
  S3 -.-> OBS
  OBS -->|"screen, redacted for the model"| AG
  OBS -->|"screen, raw, in memory"| RP
  AG -->|trace| RC
  RC -->|"capability.json"| RP
  AG --> shared
  RP --> shared
  classDef model fill:#FEF1DE,stroke:#B45309,color:#14202B
  classDef det fill:#E1F3F0,stroke:#0F766E,color:#14202B
  classDef guard fill:#E3ECF7,stroke:#1D4E89,color:#14202B
  classDef planned stroke-dasharray:5 4,fill:#ffffff,stroke:#55677A,color:#14202B
```

## 2. The model is in the loop for one phase only

```mermaid
flowchart LR
  D["<b>Discover</b><br/>observe, decide, act<br/>LLM in the loop"]:::model
  R["<b>Record</b><br/>trace to Capability<br/>status: draft"]:::det
  V["<b>Verify</b><br/>replay in a fresh browser<br/>must match the model's values<br/>status: verified"]:::det
  H["<b>Review</b><br/>a person approves<br/>status: approved (gate planned)"]:::planned
  P["<b>Replay</b><br/>deterministic, no model<br/>as often as needed"]:::det
  D --> R --> V --> H --> P
  classDef model fill:#FEF1DE,stroke:#B45309,color:#14202B
  classDef det fill:#E1F3F0,stroke:#0F766E,color:#14202B
  classDef planned stroke-dasharray:5 4,fill:#EDE7FB,stroke:#6D28D9,color:#14202B
```

## 3. A missed checkpoint is classified, not guessed at

After each step the engine needs *checkpoint met* **and** *no frame reporting HTTP >= 400*. If not, it classifies the screen using
lists carried by the artifact, in this order (so a known interstitial is never mistaken for a failure).

```mermaid
flowchart TD
  ACT["Act on the resolved control"] --> CHK{"Checkpoint met and<br/>no HTTP error status?"}
  CHK -- yes --> NEXT["Next step / read outputs"] --> FIN["Final success checkpoint"] --> OK(["success + typed outputs"])
  CHK -- no --> C1{"1. Recoverable?<br/>known pop-up, 503, expired session"}
  C1 -- yes --> H1["Bounded handler:<br/>click, backoff+reload, re-auth+restart<br/>then re-check; over budget = hard failure"]
  H1 --> CHK
  C1 -- no --> C2{"2. Business outcome?<br/>not found, restricted, control absent"}
  C2 -- yes --> O2(["status: business_outcome<br/>code + guidance, never an exception"])
  C2 -- no --> C3{"3. Failure signature?<br/>HTTP 500, raw database error"}
  C3 -- yes --> O3(["status: failed (app_error)<br/>step, expected, observed<br/>masked screenshot + DOM snapshot"])
  C3 -- no --> C4["4. Unfamiliar screen<br/>wait to the step timeout"]
  C4 --> HU{"Human available?"}
  HU -- yes --> T(["ticket: escalate"])
  HU -- no --> O4(["status: failed<br/>checkpoint_failed + evidence"])
```

## 4. Exactly one party drives the live session

The fence is enforced at the `Surface` (`perform()` / `goto()` raise `ControlViolation`), not by convention.
For approvals, `retry_step` means *approve and let automation perform it*.

```mermaid
stateDiagram-v2
  [*] --> automation
  automation --> paused: escalate (unknown screen, needs approval, no progress, model asks)
  paused --> human: operator claims the ticket
  human --> automation: hand back (next_step or retry_step), engine re-verifies checkpoint
  paused --> ended: abort or timeout
  ended --> [*]
  note right of paused
    Surface fenced.
    Ticket carries goal, step, reason,
    expected vs observed, masked screenshot.
  end note
  note right of human
    Same live browser session.
    Actions captured: kind, target, value length.
  end note
```

## 5. One artifact, per-tenant overrides that can only add

```mermaid
flowchart TB
  BASE["Base capability<br/>msc.member_savings_balance @ 1.0.0<br/>recorded once, on one tenant"]
  STALE["If the base changes:<br/>override digest differs from base digest<br/>refused until re-reviewed"]:::risk
  OV0["No override<br/>(where it was recorded)"]
  OV1["Override, add-only<br/>+ link 'Find', + label 'Avail Balance'<br/>pins the base digest"]:::guard
  OV2["Override, add-only<br/>written when drift shows up"]:::planned
  T0["Prairie: success, 0 drift"]:::det
  T1["Lakeside: success<br/>drift: s3 locator_fallback"]:::det
  T2["Tenant N: fails loudly<br/>target_not_found until overridden"]:::planned
  BASE --> OV0 --> T0
  BASE --> OV1 --> T1
  BASE --> OV2 -.-> T2
  BASE -.-> STALE
  classDef det fill:#E1F3F0,stroke:#0F766E,color:#14202B
  classDef guard fill:#E3ECF7,stroke:#1D4E89,color:#14202B
  classDef risk fill:#FBE8E8,stroke:#B91C1C,color:#14202B
  classDef planned stroke-dasharray:5 4,fill:#ffffff,stroke:#55677A,color:#14202B
```

Recorded on Prairie, the artifact fails on Lakeside with `target_not_found @ s3` (that bank labels the link **Find**). One fallback locator and one label
alias make it pass and the run reports `s3:locator_fallback`. An override cannot remove a locator, change an action or risk, or widen policy.
