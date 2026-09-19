"""Control-transfer model for human-in-the-loop escalation.

The live browser session is the shared resource; exactly one party drives it.

    owner:  automation --(escalate)--> paused --(operator claims)--> human
                 ^                                                     |
                 +-------------------(hand back)----------------------+

  * `automation` - the agent/replay engine may act.
  * `paused`     - automation has stopped and raised a ticket; nobody has claimed it yet.
                   The session is fenced: Surface.perform()/goto() raise ControlViolation.
  * `human`      - an operator claimed the ticket and is driving the *same* live
                   browser session (same cookies, same page, same position in the flow).

Fencing is enforced at the Surface (`guard`), not by convention, so a buggy
engine cannot keep clicking while a person is typing. Human actions are
captured (kinds + targets + value *lengths*, never values) and attached to the
ticket and the run log. On hand-back the operator says how the run should
continue (`next_step`: I did this step / `retry_step` / `abort`) and the engine
re-verifies its checkpoint before trusting it.

Threading rule: the browser is only ever touched by the automation thread. While a ticket is
open that thread sits in `ControlPlane._pump`, turning the browser event loop (so the human's
clicks are captured) and executing browser calls that operator threads submit.

The operator surface is pluggable (`Operator`): HTTP page, terminal prompt, or a
scripted stand-in for tests. The mechanism above is the real part.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from .evidence import RunLog

Owner = Literal["automation", "paused", "human"]
Resume = Literal["next_step", "retry_step", "abort"]
TicketState = Literal["open", "claimed", "handed_back", "abandoned", "expired"]


class ControlViolation(RuntimeError):
    """Automation tried to act while a human owns (or should own) the session."""


@dataclass
class InterventionRequest:
    ticket_id: str
    reason: str                 # needs_approval | unexpected_state | no_progress | model_requested_help | ...
    summary: str                # one sentence for the operator
    run_id: str
    capability_or_goal: str
    step_id: str | None
    url: str
    context: dict[str, Any] = field(default_factory=dict)   # redacted observation excerpt, expected vs observed
    evidence: dict[str, str] = field(default_factory=dict)  # persisted (masked) shot / snapshot paths
    created_at: float = field(default_factory=time.time)


class Ticket:
    def __init__(self, request: InterventionRequest):
        self.request = request
        self.state: TicketState = "open"
        self.operator: str | None = None
        self.resume: Resume | None = None
        self.note: str = ""
        self.human_actions: list[dict] = []
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self.live_screenshot: Callable[[], bytes] | None = None   # unmasked, served only to the operator
        self.on_claim: Callable[[], None] = lambda: None          # set by the ControlPlane

    def claim(self, operator: str = "operator") -> None:
        with self._lock:
            if self.state != "open":
                raise ControlViolation(f"ticket {self.request.ticket_id} is {self.state}, cannot claim")
            self.state, self.operator = "claimed", operator
        self.on_claim()        # ownership flips paused -> human synchronously with the claim

    def hand_back(self, resume: Resume = "next_step", note: str = "") -> None:
        with self._lock:
            if self.state != "claimed":
                raise ControlViolation(f"ticket {self.request.ticket_id} must be claimed before hand-back")
            self.state, self.resume, self.note = "handed_back", resume, note
        self._closed.set()

    def abandon(self, note: str = "") -> None:
        with self._lock:
            self.state, self.resume, self.note = "abandoned", "abort", note
        self._closed.set()

    def wait(self, timeout: float) -> bool:
        return self._closed.wait(timeout)

    def expire(self) -> None:
        with self._lock:
            if self.state in ("open", "claimed"):
                self.state, self.resume = "expired", "abort"
        self._closed.set()


class Operator(Protocol):
    """Whatever puts the ticket in front of a person and lets them claim / hand back."""
    def notify(self, ticket: Ticket) -> None: ...


class ControlPlane:
    def __init__(self, log: RunLog | None = None, operator: Operator | None = None,
                 timeout_s: float = 900.0):
        self.owner: Owner = "automation"
        self.log = log
        self.operator = operator
        self.timeout_s = timeout_s
        self.tickets: dict[str, Ticket] = {}
        # Playwright's sync API is bound to the thread that created it. Operator UIs run on other
        # threads, so anything that must touch the live browser (a live screenshot, a scripted
        # 'human') is submitted here and executed by the automation thread while it waits.
        self._calls: "queue.Queue[tuple[Callable[[], Any], Future]]" = queue.Queue()

    def call_on_owner(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        fut: Future = Future()
        self._calls.put((fn, fut))
        return fut.result(timeout)

    def _pump(self, surface, ticket: "Ticket") -> None:
        """Wait for the human without going deaf: keep the browser's event loop turning (so their
        clicks are captured) and run work operator threads submit for the browser thread."""
        deadline = time.monotonic() + self.timeout_s
        while not ticket._closed.is_set():
            if time.monotonic() > deadline:
                ticket.expire()
                break
            try:
                fn, fut = self._calls.get_nowait()
            except queue.Empty:
                surface.wait(50)
                continue
            try:
                fut.set_result(fn())
            except BaseException as exc:      # delivered to the submitting thread
                fut.set_exception(exc)
        surface.wait(50)                       # flush any last captured events

    # ---- the fence -------------------------------------------------------------------
    def assert_automation(self) -> None:
        if self.owner != "automation":
            raise ControlViolation(f"session is owned by '{self.owner}'; automation may not act")

    def _set_owner(self, owner: Owner, why: str) -> None:
        prev, self.owner = self.owner, owner
        if self.log:
            self.log.event("control_transfer", f"{prev} -> {owner}: {why}", actor="system",
                           from_owner=prev, to_owner=owner)

    # ---- escalation ---------------------------------------------------------------------
    def escalate(self, surface, request: InterventionRequest) -> Ticket:
        """Pause automation, raise the ticket, block until a human resolves it (or it expires).
        Returns the closed ticket; the caller decides how to continue from ticket.resume."""
        ticket = Ticket(request)
        ticket.live_screenshot = lambda: self.call_on_owner(lambda: surface.screenshot(masked=False))
        ticket.on_claim = lambda: self._set_owner("human", f"claimed by {ticket.operator}")
        self.tickets[request.ticket_id] = ticket
        self._set_owner("paused", f"intervention raised: {request.reason}")
        if self.log:
            self.log.event("intervention_raised", request.summary, actor="system", step=request.step_id,
                           ticket_id=request.ticket_id, reason=request.reason, url=request.url,
                           context=request.context, evidence=request.evidence)
            self.log.write_json(f"intervention-{request.ticket_id}.json", request.__dict__)
        if self.operator is None:
            ticket.expire()
            return self._finish(surface, ticket, resumed=False)
        # capture from the moment the operator is *notified*: a human may act before "claiming"
        surface.begin_human_capture()
        self.operator.notify(ticket)
        self._pump(surface, ticket)
        return self._finish(surface, ticket, resumed=True)

    def _finish(self, surface, ticket: Ticket, resumed: bool) -> Ticket:
        actions = surface.end_human_capture() if resumed else []
        ticket.human_actions = actions
        if self.log:
            self.log.event("human_actions", f"{len(actions)} human action(s) recorded", actor="human",
                           step=ticket.request.step_id, ticket_id=ticket.request.ticket_id,
                           actions=actions, note="values are never recorded, only their length")
        self._set_owner("automation" if ticket.state == "handed_back" else "paused",
                        f"ticket {ticket.state}" + (f" ({ticket.resume})" if ticket.resume else ""))
        return ticket


def new_ticket_id() -> str:
    return uuid.uuid4().hex[:8]


# ----------------------------------------------------------------------- operators
class ScriptedOperator:
    """Stand-in human for tests/demos: claims the ticket, then runs `act(surface)` against the
    same live page (as a person would with the mouse), then hands back."""
    def __init__(self, surface, act: Callable[[Any], None], control: "ControlPlane | None" = None,
                 resume: Resume = "next_step", note: str = "scripted operator"):
        self.surface, self.act, self.control, self.resume, self.note = surface, act, control, resume, note

    def notify(self, ticket: Ticket) -> None:
        def run():
            ticket.claim("scripted-operator")
            if self.control is not None:
                assert self.control.owner == "human", "claim must transfer ownership"
            try:
                if self.control is not None:
                    self.control.call_on_owner(lambda: self.act(self.surface))
                else:
                    self.act(self.surface)
            finally:
                ticket.hand_back(self.resume, self.note)
        threading.Thread(target=run, daemon=True).start()


class CliOperator:
    """Terminal operator: prints the request, waits for Enter to claim, and for a resume choice."""
    def __init__(self, input_fn: Callable[[str], str] = input, print_fn: Callable[[str], None] = print):
        self.inp, self.out = input_fn, print_fn

    def notify(self, ticket: Ticket) -> None:
        def run():
            r = ticket.request
            self.out(f"\n=== HUMAN NEEDED (ticket {r.ticket_id}) ===\nwhy: {r.reason}\n{r.summary}\n"
                     f"at: {r.url}  step: {r.step_id}\nThe browser window is the live session.")
            self.inp("Press Enter to TAKE CONTROL (automation is paused)... ")
            ticket.claim("cli-operator")
            self.out("You are in control. Do what is needed in the browser window.")
            choice = ""
            while choice not in ("n", "r", "a"):
                choice = self.inp("Done. [n]ext step (I finished it) / [r]etry step / [a]bort: ").strip().lower()[:1]
            ticket.hand_back({"n": "next_step", "r": "retry_step", "a": "abort"}[choice], "cli")
        threading.Thread(target=run, daemon=True).start()


_OPERATOR_HTML = """<!doctype html><meta charset=utf-8><title>Operator console (mock)</title>
<meta http-equiv=refresh content=4>
<style>body{font:14px system-ui;margin:24px;max-width:900px}code{background:#eee;padding:1px 4px}
.card{border:1px solid #ccc;border-radius:6px;padding:14px;margin:12px 0}img{max-width:100%;border:1px solid #999}
button{padding:6px 14px;margin-right:8px}.st{font-weight:600}</style>
<h2>Operator console <small>(mock - real transfer mechanism)</small></h2>
<p>Session driven by: <b>{owner}</b></p>{cards}"""


class HttpOperator:
    """Minimal operator web page. Deliberately bare: the *mechanism* (claim -> drive the same
    live browser -> hand back) is real; a production console would add auth, queueing,
    co-browsing and audit UI. Requires a headed browser so the human can drive the session."""
    def __init__(self, port: int = 8770):
        from flask import Flask, Response, redirect, request
        self.port = port
        self.tickets: dict[str, Ticket] = {}
        self.app = Flask("operator")
        app = self.app

        @app.get("/")
        def index():
            cards = ""
            for t in list(self.tickets.values())[::-1]:
                r = t.request
                buttons = ""
                if t.state == "open":
                    buttons = f'<form method=post action="/t/{r.ticket_id}/claim"><button>Take control</button></form>'
                elif t.state == "claimed":
                    buttons = (f'<form method=post action="/t/{r.ticket_id}/back">'
                               f'<select name=resume><option value=next_step>I completed the step - continue after it</option>'
                               f'<option value=retry_step>Retry the step</option><option value=abort>Abort the run</option></select> '
                               f'<input name=note placeholder="note"> <button>Hand back</button></form>')
                shot = f'<img src="/t/{r.ticket_id}/shot.png?{time.time()}">' if t.state in ("open", "claimed") else ""
                cards += (f'<div class=card><div class=st>[{t.state}] {r.reason} &mdash; ticket {r.ticket_id}</div>'
                          f'<p>{r.summary}</p><p>run <code>{r.run_id}</code> &middot; step <code>{r.step_id}</code> &middot; '
                          f'<code>{r.url}</code></p>{shot}{buttons}</div>')
            states = {t.state for t in self.tickets.values()}
            owner = "human" if "claimed" in states else "nobody (automation paused)" if "open" in states else "automation"
            return _OPERATOR_HTML.format(owner=owner, cards=cards or "<p>No open requests.</p>")

        @app.post("/t/<tid>/claim")
        def claim(tid):
            self.tickets[tid].claim("http-operator")
            return redirect("/")

        @app.post("/t/<tid>/back")
        def back(tid):
            self.tickets[tid].hand_back(request.form.get("resume", "next_step"), request.form.get("note", ""))
            return redirect("/")

        @app.get("/t/<tid>/shot.png")
        def shot(tid):
            t = self.tickets[tid]
            return Response(t.live_screenshot() if t.live_screenshot else b"", mimetype="image/png")

    def notify(self, ticket: Ticket) -> None:
        self.tickets[ticket.request.ticket_id] = ticket
        if not getattr(self, "_started", False):
            threading.Thread(target=lambda: self.app.run(host="127.0.0.1", port=self.port, threaded=True),
                             daemon=True).start()
            self._started = True
        print(f"[operator] ticket {ticket.request.ticket_id}: open http://127.0.0.1:{self.port}/ ({ticket.request.reason})")


def ticket_summary(t: Ticket) -> dict:
    return {"ticket": t.request.ticket_id, "state": t.state, "resume": t.resume, "note": t.note,
            "actions": len(t.human_actions)}

