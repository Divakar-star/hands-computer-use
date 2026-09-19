"""The mock console is test infrastructure, but the fault injection has to be
trustworthy: replay tests are only meaningful if 'not found' really means not
found and 'expired' really expires. These pin its behaviour."""
import pytest

from mockbank import create_app
from mockbank.faults import Faults, parse_spec

USER = {"USRID": "teller01", "PWD": "demo-not-a-real-password"}


def make(tenant="prairie", **faults):
    app = create_app(tenant, Faults(**faults))
    c = app.test_client()
    assert c.post("/msc/login", data=USER).status_code == 302
    return app, c


def find(c, number, **extra):
    return c.post("/msc/inq/find", data={"f_mbr": number, **extra})


def test_bad_login_is_rejected():
    c = create_app().test_client()
    r = c.post("/msc/login", data={"USRID": "x", "PWD": "y"})
    assert r.status_code == 200 and b"Invalid user ID or password" in r.data


def test_unauthenticated_request_redirects_to_login_with_timeout_notice():
    c = create_app().test_client()
    r = c.get("/msc/inq")
    assert r.status_code == 302 and r.headers["Location"].endswith("/msc/login?expired=1")


def test_frameset_shell_has_three_frames_and_no_test_ids():
    _, c = make()
    html = c.get("/msc/main").data.decode()
    assert html.count("<frame ") == 3 and "data-testid" not in html


def test_happy_path_search_member_account_balance():
    _, c = make()
    r = find(c, "12345")
    assert r.status_code == 302 and r.headers["Location"] == "/msc/member/12345"
    member = c.get("/msc/member/12345").data.decode()
    assert "Dana Whitfield" in member and "900-12-3456" in member  # legacy shows raw PII
    acct = c.get("/msc/acct/12345-S01").data.decode()
    assert "$4,821.37" in acct and "$4,721.37" in acct


def test_validation_error_is_http_200_with_message():
    _, c = make()
    r = find(c, "12ab")
    assert r.status_code == 200 and b"must be exactly 5 digits" in r.data


def test_not_found_is_a_normal_page_not_an_error_status():
    _, c = make()
    r = find(c, "99999")
    assert r.status_code == 200 and b"No member record found" in r.data


def test_restricted_member_is_permission_denied():
    _, c = make()
    r = find(c, "77777")
    assert r.status_code == 200 and b"ERR-403-R" in r.data
    assert b"ERR-403-R" in c.get("/msc/member/77777").data  # not bypassable by URL


def test_member_without_savings_has_no_savings_row():
    _, c = make()
    assert b"Share Savings" not in c.get("/msc/member/40551").data


def test_reports_area_denied_for_teller():
    _, c = make()
    assert b"ERR-403" in c.get("/msc/reports").data


def test_tenant_variant_changes_labels_not_structure():
    _, c = make("lakeside")
    html = c.get("/msc/inq").data.decode()
    assert "Cust. #" in html and 'name="f_mbr"' in html
    assert "Regular Savings" in c.get("/msc/member/12345").data.decode()
    assert "Avail Balance" in c.get("/msc/acct/12345-S01").data.decode()


# ---------------------------------------------------------------- fault injection
def test_notice_interstitial_appears_once_then_lookup_continues():
    _, c = make(notice=True)
    first = find(c, "12345")
    assert first.status_code == 200 and b"I Acknowledge" in first.data
    ack = find(c, "12345", ack="1")
    assert ack.status_code == 302
    assert find(c, "12345").status_code == 302  # session remembers the ack


def test_flaky_member_load_fails_then_recovers():
    _, c = make(flaky_member=2)
    assert c.get("/msc/member/12345").status_code == 503
    assert c.get("/msc/member/12345").status_code == 503
    assert c.get("/msc/member/12345").status_code == 200


def test_app_error_on_account_page_is_http_500_with_raw_detail():
    _, c = make(app_error_acct=True)
    r = c.get("/msc/acct/12345-S01")
    assert r.status_code == 500 and b"ORA-00942" in r.data


def test_session_expires_after_n_page_loads():
    _, c = make(expire_after=2)
    assert c.get("/msc/inq").status_code == 200
    assert find(c, "12345").status_code == 302
    r = c.get("/msc/member/12345")
    assert r.status_code == 302 and "expired=1" in r.headers["Location"]


def test_admin_can_expire_live_sessions():
    app, c = make()
    assert c.get("/msc/inq").status_code == 200
    assert c.post("/__admin/expire_sessions", environ_base={"REMOTE_ADDR": "127.0.0.1"}).json["expired"] == 1
    assert c.get("/msc/inq").status_code == 302


def test_admin_is_localhost_only():
    c = create_app().test_client()
    r = c.get("/__admin/state", environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert r.status_code == 403


def test_parse_spec():
    f = parse_spec("notice,slow_ms=250,flaky_member=1")
    assert f.notice is True and f.slow_ms == 250 and f.flaky_member == 1
    with pytest.raises(KeyError):
        parse_spec("nonsense")


# ---------------------------------------------------------------- open sub-account
GOOD = {"o_mbr": "12345", "o_typ": "SAV", "o_dep": "25.00", "o_nick": "Vacation",
        "o_src": "chk", "o_cns": "1"}


def review(c, **over):
    return c.post("/msc/open/review", data={**GOOD, **over})


@pytest.mark.parametrize("over,msg", [
    ({"o_mbr": "1"}, b"exactly 5 digits"),
    ({"o_mbr": "99999"}, b"No member record"),
    ({"o_typ": ""}, b"Select an account type"),
    ({"o_dep": "abc"}, b"dollar amount"),
    ({"o_dep": "2.00"}, b"at least $5.00"),
    ({"o_nick": "x" * 21}, b"20 characters"),
    ({"o_src": ""}, b"funding source"),
    ({"o_cns": ""}, b"consent"),
])
def test_open_validation_errors(over, msg):
    _, c = make()
    assert msg in review(c, **over).data


def test_review_screen_does_not_commit_anything():
    app, c = make()
    r = review(c)
    assert b"Confirm New Sub-Account" in r.data and b"cannot be reversed" in r.data
    assert app.config["MSC_STATE"].commits == []


def test_commit_is_irreversible_single_use_and_needs_token():
    app, c = make()
    st = app.config["MSC_STATE"]
    html = review(c).data.decode()
    tok = html.split('name="tok" value="')[1].split('"')[0]
    assert b"ERR-409" in c.post("/msc/open/commit", data={"tok": "forged"}).data
    assert b"MSC-000101" in c.post("/msc/open/commit", data={"tok": tok}).data
    assert len(st.commits) == 1 and st.commits[0]["account"] == "12345-S02"
    assert b"ERR-409" in c.post("/msc/open/commit", data={"tok": tok}).data  # replay refused
    assert len(st.commits) == 1
