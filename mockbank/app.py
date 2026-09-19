"""Mock legacy core-banking console ("MSC") - the proxy target for the project.

Deliberately hostile to selector-based automation, like the real long tail:
  * a <frameset> shell (header / nav / body frames),
  * table-based layout, <font> tags, no semantic landmarks,
  * no test IDs, no element ids, cryptic field names, labels that are not
    programmatically associated with their inputs,
  * some actions are <a href="javascript:..."> links rather than buttons,
  * business errors come back as HTTP 200 pages with red text, not status codes,
  * an expired session drops the login page into whichever frame was loading.

It also has real runtime states a replay must handle (see faults.py):
validation errors, record-not-found, permission denial, a mid-flow compliance
interstitial, transient 503s, slowness, session timeout and a raw app error.

Everything is synthetic. Do not point real credentials or PII at it.
"""
from __future__ import annotations

import argparse
import copy
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from flask import Flask, jsonify, make_response, redirect, render_template, request

from .data import Account, Member, Txn, account_number, build_members
from .faults import Faults, parse_spec
from .tenants import OPENABLE, TENANTS

COOKIE = "MSCSID"
# Frameset plumbing does not count as a "page load" for session-expiry purposes.
_UNCOUNTED = {"/msc/main", "/msc/hdr", "/msc/nav"}
_OPEN = ("/msc/login", "/msc/logoff")


@dataclass
class Session:
    sid: str
    user: str
    role: str
    started: str
    last_seen: float
    pages: int = 0
    notice_ack: bool = False
    open_token: str | None = None
    open_draft: dict | None = None


@dataclass
class State:
    tenant: str
    ttl: int
    faults: Faults
    members: dict[str, Member] = field(default_factory=build_members)
    sessions: dict[str, Session] = field(default_factory=dict)
    commits: list[dict] = field(default_factory=list)  # irreversible actions actually executed
    lock: threading.Lock = field(default_factory=threading.Lock)
    conf_seq: int = 100


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def create_app(tenant: str = "prairie", faults: Faults | None = None,
               session_ttl: int = 900) -> Flask:
    if tenant not in TENANTS:
        raise ValueError(f"unknown tenant {tenant!r}; choose from {sorted(TENANTS)}")
    app = Flask(__name__)
    st = State(tenant=tenant, ttl=session_ttl, faults=faults or Faults())
    app.config["MSC_STATE"] = st
    user_ok = os.environ.get("MSC_USER", "teller01")
    pass_ok = os.environ.get("MSC_PASS", "demo-not-a-real-password")
    T = TENANTS[tenant]

    @app.context_processor
    def _ctx():
        return {"T": T, "money": money}

    # ------------------------------------------------------------------ session gate
    def current_session() -> Session | None:
        sid = request.cookies.get(COOKIE)
        s = st.sessions.get(sid) if sid else None
        if s is None:
            return None
        now = time.time()
        by_fault = bool(st.faults.expire_after and s.pages >= st.faults.expire_after)
        if now - s.last_seen > st.ttl or by_fault:
            if by_fault:
                st.faults.expire_after = 0   # one-shot: a timeout is an event, not a permanent state
            st.sessions.pop(sid, None)
            return None
        s.last_seen = now
        return s

    @app.before_request
    def gate():
        path = request.path
        if path.startswith("/__admin") or path in _OPEN or path == "/":
            return None
        s = current_session()
        if s is None:
            resp = redirect("/msc/login?expired=1")
            resp.delete_cookie(COOKIE)
            return resp
        if path not in _UNCOUNTED:
            s.pages += 1
        request.msc_session = s  # type: ignore[attr-defined]
        return None

    def sess() -> Session:
        return request.msc_session  # type: ignore[attr-defined]

    def lag() -> None:
        if st.faults.slow_ms:
            time.sleep(st.faults.slow_ms / 1000)

    # ------------------------------------------------------------------ login / shell
    @app.get("/")
    def root():
        return redirect("/msc/login")

    @app.route("/msc/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if (request.form.get("USRID") == user_ok
                    and request.form.get("PWD") == pass_ok):
                sid = secrets.token_hex(16)
                st.sessions[sid] = Session(
                    sid=sid, user=user_ok, role="teller",
                    started=datetime.now().strftime("%H:%M:%S"), last_seen=time.time())
                resp = redirect("/msc/main")
                resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax")
                return resp
            error = "Invalid user ID or password."
        return render_template("login.html", error=error,
                               expired=bool(request.args.get("expired")))

    @app.get("/msc/logoff")
    def logoff():
        st.sessions.pop(request.cookies.get(COOKIE, ""), None)
        resp = redirect("/msc/login")
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/msc/main")
    def main_frameset():
        return render_template("frameset.html")

    @app.get("/msc/hdr")
    def header():
        return render_template("hdr.html", s=sess(), now=datetime.now().strftime("%m/%d/%Y %H:%M"))

    @app.get("/msc/nav")
    def nav():
        return render_template("nav.html")

    @app.get("/msc/home")
    def home():
        return render_template("home.html")

    @app.get("/msc/reports")
    def reports():
        # Role-based denial delivered the legacy way: HTTP 200 + red text.
        return render_template("denied.html", code="ERR-403", area=T["nav_reports"])

    # ------------------------------------------------------------------ member inquiry
    @app.get("/msc/inq")
    def inquiry():
        return render_template("inq.html", error=None, value="")

    @app.post("/msc/inq/find")
    def inquiry_find():
        value = (request.form.get("f_mbr") or "").strip()
        if not re.fullmatch(r"\d{5}", value):
            return render_template("inq.html", value=value,
                                   error=f"{T['member_label']} must be exactly 5 digits.")
        if st.faults.notice and not sess().notice_ack:
            if request.form.get("ack") == "1":
                sess().notice_ack = True
            else:
                return render_template("notice.html", value=value)
        member = st.members.get(value)
        if member is None:
            return render_template("inq.html", value=value,
                                   error=f"No member record found for {T['member_label']} {value}.")
        if member.restricted:
            return render_template("denied.html", code="ERR-403-R",
                                   area=f"record {value}")
        return redirect(f"/msc/member/{value}")

    def load_member(number: str):
        lag()
        if st.faults.flaky_member > 0:
            st.faults.flaky_member -= 1
            return None, (render_template("unavailable.html"), 503)
        member = st.members.get(number)
        if member is None:
            return None, render_template("inq.html", value=number,
                                         error=f"No member record found for {T['member_label']} {number}.")
        if member.restricted:
            return None, render_template("denied.html", code="ERR-403-R", area=f"record {number}")
        return member, None

    @app.get("/msc/member/<number>")
    def member_page(number: str):
        member, err = load_member(number)
        if err is not None:
            return err
        return render_template("member.html", m=member)

    @app.get("/msc/acct/<acct_no>")
    def account_page(acct_no: str):
        lag()
        if st.faults.app_error_acct:
            body = render_template("apperror.html", detail="ORA-00942: table or view does not exist "
                                   "(ACCT_BAL_VW) at line 88 of pkg_acct_inq")
            return body, 500
        number = acct_no.split("-")[0]
        member = st.members.get(number)
        acct = next((a for a in (member.accounts if member else []) if a.number == acct_no), None)
        if member is None or acct is None or member.restricted:
            return render_template("inq.html", value=number,
                                   error=f"Account {acct_no} was not found.")
        return render_template("acct.html", m=member, a=acct)

    # ------------------------------------------------------------------ open sub-account
    @app.get("/msc/open")
    def open_form():
        return render_template("open.html", error=None, f={}, openable=OPENABLE)

    @app.post("/msc/open/review")
    def open_review():
        f = {k: (request.form.get(k) or "").strip() for k in
             ("o_mbr", "o_typ", "o_dep", "o_nick", "o_src")}
        f["o_cns"] = request.form.get("o_cns") == "1"

        def bad(msg: str):
            return render_template("open.html", error=msg, f=f, openable=OPENABLE)

        if not re.fullmatch(r"\d{5}", f["o_mbr"]):
            return bad(f"{T['member_label']} must be exactly 5 digits.")
        member = st.members.get(f["o_mbr"])
        if member is None:
            return bad(f"No member record found for {T['member_label']} {f['o_mbr']}.")
        if member.restricted:
            return render_template("denied.html", code="ERR-403-R", area=f"record {f['o_mbr']}")
        if f["o_typ"] not in OPENABLE:
            return bad("Select an account type.")
        try:
            dep = round(float(f["o_dep"].replace("$", "").replace(",", "")) * 100)
        except ValueError:
            return bad("Initial deposit must be a dollar amount.")
        if dep < 500:
            return bad("Initial deposit must be at least $5.00.")
        if len(f["o_nick"]) > 20:
            return bad("Nickname may not exceed 20 characters.")
        if f["o_src"] not in ("chk", "cash"):
            return bad("Select a funding source.")
        if not f["o_cns"]:
            return bad("Member consent and disclosures must be confirmed.")
        s = sess()
        s.open_token = secrets.token_hex(8)
        s.open_draft = {"member": member.number, "product": f["o_typ"], "cents": dep,
                        "nick": f["o_nick"], "src": f["o_src"]}
        return render_template("open_review.html", m=member, d=s.open_draft, tok=s.open_token,
                               src_label="Existing share draft" if f["o_src"] == "chk" else "Cash")

    @app.post("/msc/open/commit")
    def open_commit():
        """IRREVERSIBLE: creates the account. Automation must never reach this unattended."""
        s = sess()
        tok = request.form.get("tok")
        if not tok or tok != s.open_token or not s.open_draft:
            return render_template("denied.html", code="ERR-409", area="this request (stale or duplicate)")
        d, s.open_draft, s.open_token = s.open_draft, None, None
        with st.lock:
            member = st.members[d["member"]]
            seq = sum(1 for a in member.accounts if a.product == d["product"]) + 1
            number = account_number(member.number, d["product"], seq)
            member.accounts.append(Account(number=number, product=d["product"],
                                           current_cents=d["cents"], available_cents=d["cents"],
                                           last_activity=datetime.now().strftime("%Y-%m-%d"),
                                           txns=[Txn(datetime.now().strftime("%Y-%m-%d"),
                                                     "Opening deposit", d["cents"])]))
            st.conf_seq += 1
            conf = f"MSC-{st.conf_seq:06d}"
            st.commits.append({"confirmation": conf, "account": number, **d})
        return render_template("open_done.html", m=member, number=number, conf=conf)

    # ------------------------------------------------------------------ test-harness admin
    def local_only():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify(error="localhost only"), 403
        return None

    @app.route("/__admin/faults", methods=["GET", "POST"])
    def admin_faults():
        if (deny := local_only()):
            return deny
        if request.method == "POST":
            try:
                st.faults.update(request.get_json(force=True) or {})
            except (KeyError, ValueError) as exc:
                return jsonify(error=str(exc)), 400
        return jsonify(st.faults.as_dict())

    @app.post("/__admin/expire_sessions")
    def admin_expire():
        if (deny := local_only()):
            return deny
        n = len(st.sessions)
        st.sessions.clear()
        return jsonify(expired=n)

    @app.post("/__admin/reset")
    def admin_reset():
        if (deny := local_only()):
            return deny
        st.faults = Faults()
        st.sessions.clear()
        st.commits.clear()
        st.members = build_members()
        return jsonify(ok=True)

    @app.get("/__admin/state")
    def admin_state():
        """What a verifier can check: which irreversible actions really ran."""
        if (deny := local_only()):
            return deny
        return jsonify(tenant=st.tenant, faults=st.faults.as_dict(),
                       sessions=len(st.sessions), commits=copy.deepcopy(st.commits))

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


def main() -> None:
    p = argparse.ArgumentParser(description="Mock legacy banking console (synthetic data only)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--tenant", choices=sorted(TENANTS), default="prairie")
    p.add_argument("--faults", default=os.environ.get("MSC_FAULTS", ""),
                   help="preset faults, e.g. 'notice,slow_ms=2500,flaky_member=1'")
    p.add_argument("--session-ttl", type=int, default=900)
    args = p.parse_args()
    app = create_app(args.tenant, parse_spec(args.faults), args.session_ttl)
    print(f"MSC mock console [{args.tenant}] on http://127.0.0.1:{args.port}/  "
          f"(login: {os.environ.get('MSC_USER', 'teller01')} / "
          f"{os.environ.get('MSC_PASS', 'demo-not-a-real-password')})")
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
