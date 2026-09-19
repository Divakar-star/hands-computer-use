"""Synthetic member data for the mock console. Everything here is fake.

SSNs use the 9xx area range (never issued), phones use the 555-01xx fiction
range, and addresses are invented. Money is stored as integer cents.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Product codes are tenant-neutral; each tenant renders its own product name.
SAVINGS, CHECKING, MONEY_MARKET, CERTIFICATE = "SAV", "CHK", "MMK", "CD"
_SUFFIX = {SAVINGS: "S", CHECKING: "C", MONEY_MARKET: "M", CERTIFICATE: "T"}


@dataclass
class Txn:
    date: str
    desc: str
    cents: int


@dataclass
class Account:
    number: str
    product: str
    current_cents: int
    available_cents: int
    status: str = "Active"
    last_activity: str = ""
    txns: list[Txn] = field(default_factory=list)


@dataclass
class Member:
    number: str
    name: str
    dob: str
    ssn: str
    phone: str
    address: str
    status: str = "Active"
    restricted: bool = False  # staff/employee record: teller role may not view
    accounts: list[Account] = field(default_factory=list)


def account_number(member: str, product: str, seq: int) -> str:
    return f"{member}-{_SUFFIX[product]}{seq:02d}"


def _acct(member: str, product: str, seq: int, cur: int, avail: int | None = None,
          last: str = "2026-09-12", txns: list[tuple[str, str, int]] | None = None) -> Account:
    return Account(
        number=account_number(member, product, seq),
        product=product,
        current_cents=cur,
        available_cents=cur if avail is None else avail,
        last_activity=last,
        txns=[Txn(*t) for t in (txns or [])],
    )


def build_members() -> dict[str, Member]:
    """Fresh copy per app instance so tests / runs never share mutations."""
    members = [
        Member(
            "12345", "Dana Whitfield", "1984-03-17", "900-12-3456", "(555) 010-0142",
            "1420 Elm Street, Springfield, IL 62704",
            accounts=[
                _acct("12345", SAVINGS, 1, 482137, 472137, "2026-09-14", [
                    ("2026-09-14", "Dividend credit", 1206),
                    ("2026-09-09", "Transfer from checking", 50000),
                    ("2026-08-30", "ATM withdrawal hold", -10000),
                ]),
                _acct("12345", CHECKING, 1, 120355, None, "2026-09-16", [
                    ("2026-09-16", "POS purchase", -4218),
                    ("2026-09-15", "Payroll deposit", 184500),
                ]),
                _acct("12345", MONEY_MARKET, 1, 1500000, None, "2026-09-01"),
            ],
        ),
        Member(
            "20817", "Marcus Oyelaran", "1991-11-02", "900-55-1187", "(555) 010-0177",
            "88 Harbor Lane, Madison, WI 53703",
            accounts=[
                _acct("20817", SAVINGS, 1, 31208, None, "2026-09-11", [
                    ("2026-09-11", "Deposit", 5000),
                ]),
                _acct("20817", CHECKING, 1, 9840, None, "2026-09-17"),
                _acct("20817", CERTIFICATE, 1, 1000000, None, "2026-06-01"),
            ],
        ),
        Member(
            "33190", "Priya Raman", "1978-07-25", "900-87-2201", "(555) 010-0163",
            "17 Cedar Court, Ann Arbor, MI 48104",
            accounts=[
                _acct("33190", SAVINGS, 1, 2745590, 2745590, "2026-09-15", [
                    ("2026-09-15", "Dividend credit", 5622),
                ]),
                _acct("33190", CHECKING, 1, 351120, None, "2026-09-17"),
            ],
        ),
        # Edge case for "read the savings balance": a member with no savings account.
        Member(
            "40551", "Tom Becker", "1969-01-09", "900-33-9042", "(555) 010-0119",
            "302 Ridge Road, Dayton, OH 45402",
            accounts=[_acct("40551", CHECKING, 1, 75011, None, "2026-09-16")],
        ),
        # Staff record: the teller role is not authorised to open it.
        Member(
            "77777", "Restricted Record", "1980-01-01", "900-00-0000", "(555) 010-0100",
            "n/a", restricted=True,
        ),
    ]
    return {m.number: m for m in members}
