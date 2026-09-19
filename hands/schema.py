"""The capability artifact and the replay result contract.

A Capability is what discovery produces and what replay consumes. It is
deliberately decoupled from the model transcript: it says *what the flow is*
(steps, how each control is found, what goes in, what comes out, how success
is verified, which non-happy-path states are expected) and never *how the
model reasoned*. Everything a calling agent or a reviewer needs is in here.

Design rules the shapes below encode:
  * Controls are addressed by an ordered list of independent locator strategies
    (never one selector), each tagged with its stability. Replay cross-checks them.
  * Inputs/outputs are typed and carry a `sensitivity` so logging/redaction is
    driven by the contract instead of guesswork.
  * Business outcomes, recoverable conditions and hard-failure signatures are three
    separate lists, because they demand three different responses.
  * Risk is declared per step and re-derived at runtime; the declaration is a
    reviewable claim, not the enforcement.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionType(str, Enum):
    navigate = "navigate"
    click = "click"
    fill = "fill"
    select = "select"
    check = "check"
    press = "press"
    extract = "extract"
    wait = "wait"


class Risk(str, Enum):
    safe = "safe"                  # navigation / reads: no state change
    reversible = "reversible"      # form entry; nothing committed until a later step
    irreversible = "irreversible"  # commits / posts / deletes: needs explicit approval
    forbidden = "forbidden"        # never automated (sign-off, admin endpoints)


_RISK_ORDER = [Risk.safe, Risk.reversible, Risk.irreversible, Risk.forbidden]


def max_risk(*risks: Risk) -> Risk:
    return max(risks, key=_RISK_ORDER.index, default=Risk.safe)


class Sensitivity(str, Enum):
    none = "none"
    identifier = "identifier"  # member / account numbers: masked in logs, never an example value
    financial = "financial"    # balances etc.: returned to the caller, masked in persisted logs
    pii = "pii"                # names, SSNs, DOB, addresses: never persisted
    secret = "secret"          # credentials/tokens: never persisted, never given to a model


class ValueType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"   # carried as a string to avoid float error on money
    boolean = "boolean"
    date = "date"


# --------------------------------------------------------------------------- contract
class Param(_M):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ValueType = ValueType.string
    description: str
    required: bool = True
    pattern: str | None = None          # regex the value must fully match
    enum: list[str] | None = None
    sensitivity: Sensitivity = Sensitivity.none
    example: str | None = None

    @model_validator(mode="after")
    def _no_example_for_sensitive(self):
        if self.example is not None and self.sensitivity != Sensitivity.none:
            raise ValueError(f"param {self.name!r}: example not allowed for sensitivity={self.sensitivity.value}")
        return self


class OutputSpec(_M):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ValueType = ValueType.string
    description: str
    sensitivity: Sensitivity = Sensitivity.financial
    required: bool = True


class Value(_M):
    """Either a caller-supplied parameter or a constant baked into the flow."""
    param: str | None = None
    literal: str | bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.param is None) == (self.literal is None):
            raise ValueError("Value needs exactly one of param / literal")
        return self


# --------------------------------------------------------------------------- targeting
LocatorKind = Literal["field_name", "href", "role_name", "visible_text", "structural"]


class Locator(_M):
    kind: LocatorKind
    role: str | None = None      # role_name / structural
    name: str | None = None      # role_name: accessible name; visible_text: text
    value: str | None = None     # field_name: name attr; href: path glob, {{param}} allowed
    group: str | None = None     # disambiguating row/group label (radio buttons etc.)
    ordinal: int | None = None   # structural: index among same-role controls in the frame
    stability: Literal["high", "medium", "low"] = "medium"
    note: str | None = None


class Target(_M):
    description: str                 # human-readable, for reviewers
    frame: str | None = None         # frame name hint (legacy framesets)
    role: str
    locators: list[Locator] = Field(min_length=1)   # ordered, most robust first
    robustness: str = ""             # why this ordering (reasoning kept with the artifact)


# --------------------------------------------------------------------------- conditions
class Condition(_M):
    """A detector over the observed state of the surface. Used for step
    checkpoints, business outcomes, recoverable conditions and failure signatures."""
    kind: Literal["text_contains", "text_regex", "frame_url", "status_in",
                  "element_present", "all", "any", "not"]
    frame: str | None = None
    value: str | None = None
    statuses: list[int] | None = None
    target: Target | None = None
    of: list["Condition"] = Field(default_factory=list)
    describe: str | None = None


Condition.model_rebuild()


class Extraction(_M):
    """Read a value off the page. `labeled_value` = the cell beside a label cell,
    the dominant layout in legacy business apps."""
    kind: Literal["labeled_value"] = "labeled_value"
    output: str
    labels: list[str] = Field(min_length=1)   # accepted spellings (tenant aliases go here)
    frame: str | None = None


class Step(_M):
    id: str
    intent: str
    action: ActionType
    target: Target | None = None
    value: Value | None = None
    risk: Risk = Risk.safe
    expect: Condition | None = None            # postcondition, verified before moving on
    extractions: list[Extraction] = Field(default_factory=list)
    on_target_missing: str | None = None       # outcome code: 'control absent' is a business answer
    timeout_ms: int = 8000


# --------------------------------------------------------------------------- non-happy paths
class Outcome(_M):
    """EXPECTED business result the caller needs (e.g. 'no such member'). Not an error.
    `when` is a detector over the screen; it may be omitted for an outcome that is raised only by a
    step's `on_target_missing` rule (e.g. the control for 'savings account' does not exist)."""
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    when: Condition | None = None
    caller_guidance: str = ""


class Handler(_M):
    kind: Literal["click", "reload_frame", "wait_retry", "reauth"]
    target: Target | None = None     # click
    frame: str | None = None         # reload_frame
    delay_ms: int = 500              # wait_retry / reload_frame backoff base


class Recoverable(_M):
    """Known transient/interstitial condition with a deliberate, bounded response."""
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    when: Condition
    handler: Handler
    max_times: int = 2


class FailureSignature(_M):
    """Recognisable hard failure (raw app error, permission denial not declared as an outcome...)."""
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    when: Condition
    retriable: bool = False


# --------------------------------------------------------------------------- the artifact
class AppRef(_M):
    product: str                      # vendor product id, shared by many tenants
    product_version_seen: str | None = None
    tenant_recorded_on: str | None = None
    origin_hint: str | None = None    # informational only; the allowlist is enforced by policy


class Provenance(_M):
    run_id: str
    recorded_at: str
    goal: str
    model: str
    discovery_steps: int
    human_assisted: bool = False
    transcript_ref: str | None = None  # path/hash of the (redacted) run log; transcript is NOT embedded


class Verification(_M):
    passed: bool
    run_id: str
    at: str
    note: str = ""


class ReviewSummary(_M):
    max_risk: Risk
    irreversible_steps: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)   # names of outputs
    notes: list[str] = Field(default_factory=list)


class Capability(_M):
    schema_version: str = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")          # e.g. msc.member_savings_balance
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["draft", "verified", "approved", "deprecated"] = "draft"
    name: str
    description: str                  # written for a calling agent: when to use this
    app: AppRef
    entry: str                        # path the flow starts from (post-login)
    inputs: list[Param] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success: Condition                # final checkpoint
    outcomes: list[Outcome] = Field(default_factory=list)
    recoverables: list[Recoverable] = Field(default_factory=list)
    failure_signatures: list[FailureSignature] = Field(default_factory=list)
    review: ReviewSummary
    provenance: Provenance | None = None
    verification: Verification | None = None
    digest: str | None = None         # sha256 over the canonical body; detects tampering / drift in review

    @field_validator("steps")
    @classmethod
    def _unique_step_ids(cls, v: list[Step]):
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        return v

    @model_validator(mode="after")
    def _references_resolve(self):
        pnames = {p.name for p in self.inputs}
        onames = {o.name for o in self.outputs}
        for s in self.steps:
            if s.value and s.value.param and s.value.param not in pnames:
                raise ValueError(f"step {s.id}: unknown param {s.value.param!r}")
            for e in s.extractions:
                if e.output not in onames:
                    raise ValueError(f"step {s.id}: extraction into undeclared output {e.output!r}")
        codes = {o.code for o in self.outcomes}
        for s in self.steps:
            if s.on_target_missing and s.on_target_missing not in codes:
                raise ValueError(f"step {s.id}: on_target_missing references undeclared outcome {s.on_target_missing!r}")
        extracted = {e.output for s in self.steps for e in s.extractions}
        for o in self.outputs:
            if o.required and o.name not in extracted:
                raise ValueError(f"output {o.name!r} is declared but no step extracts it")
        return self

    # ---- agent-facing view --------------------------------------------------------------
    def to_tool_spec(self) -> dict[str, Any]:
        """What a calling AI agent sees: a function-calling tool + the result contract."""
        props: dict[str, Any] = {}
        for p in self.inputs:
            js: dict[str, Any] = {"description": p.description}
            js["type"] = {"integer": "integer", "boolean": "boolean"}.get(p.type.value, "string")
            if p.pattern:
                js["pattern"] = p.pattern
            if p.enum:
                js["enum"] = p.enum
            props[p.name] = js
        return {
            "name": self.id.replace(".", "__"),
            "description": self.description,
            "input_schema": {"type": "object", "properties": props,
                             "required": [p.name for p in self.inputs if p.required],
                             "additionalProperties": False},
            "returns": {
                "outputs": {o.name: {"type": o.type.value, "description": o.description,
                                     "sensitivity": o.sensitivity.value} for o in self.outputs},
                "business_outcomes": {o.code: {"description": o.description,
                                               "guidance": o.caller_guidance} for o in self.outcomes},
                "statuses": ["success", "business_outcome", "blocked", "escalated", "failed"],
            },
            "max_risk": self.review.max_risk.value,
            "status": self.status,
            "version": self.version,
        }


# --------------------------------------------------------------------------- result contract
class FailureCategory(str, Enum):
    invalid_input = "invalid_input"            # caller error; nothing was touched
    precondition_failed = "precondition_failed"  # could not establish the session / entry state
    session_expired = "session_expired"
    target_not_found = "target_not_found"
    target_ambiguous = "target_ambiguous"
    checkpoint_failed = "checkpoint_failed"    # acted, but the expected state never appeared
    unexpected_state = "unexpected_state"      # unknown page / dialog we have no handler for
    app_error = "app_error"                    # raw application/host error
    timeout = "timeout"
    policy_violation = "policy_violation"
    handoff_failed = "handoff_failed"
    internal_error = "internal_error"


class Failure(_M):
    category: FailureCategory
    step_id: str | None = None
    message: str
    expected: str | None = None
    observed: str | None = None
    retriable: bool = False
    evidence: dict[str, str] = Field(default_factory=dict)   # kind -> path


class Recovery(_M):
    code: str
    step_id: str | None = None
    action: str
    attempts: int = 1


class Drift(_M):
    step_id: str
    kind: Literal["locator_fallback", "strategy_disagreement", "frame_changed", "label_alias"]
    detail: str


class Handoff(_M):
    ticket_id: str
    reason: str
    step_id: str | None = None
    resolution: str            # next_step | retry_step | abort | timed_out
    human_actions: int = 0


class Result(_M):
    run_id: str
    capability_id: str
    capability_version: str
    tenant: str | None = None
    status: Literal["success", "business_outcome", "blocked", "escalated", "failed"]
    outcome: str | None = None                   # business outcome code
    outputs: dict[str, Any] = Field(default_factory=dict)
    failure: Failure | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    drift: list[Drift] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    steps_completed: int = 0
    duration_ms: int = 0
    message: str = ""
