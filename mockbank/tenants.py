"""Two 'tenants' running the same vendor product (MSC), configured differently.

Same page structure and workflow, different branding, labels and product
names. This is the stand-in for the real-world case where hundreds of
institutions run one vendor app with per-institution configuration, and it
lets us show one artifact being reused across variants.
"""
from __future__ import annotations

TENANTS: dict[str, dict] = {
    "prairie": {
        "brand": "Prairie Federal Credit Union",
        "console": "Member Servicing Console",
        "version": "MSC 7.2.1",
        "accent": "#1f4e79",
        "nav_home": "Home",
        "nav_inq": "Member Inquiry",
        "nav_open": "Open Sub-Account",
        "nav_reports": "Reports",
        "member_label": "Member Number",
        "search_btn": "Search",
        "cur_label": "Current Balance",
        "avail_label": "Available Balance",
        "products": {
            "SAV": "Share Savings",
            "CHK": "Share Draft Checking",
            "MMK": "Money Market",
            "CD": "Share Certificate",
        },
    },
    "lakeside": {
        "brand": "Lakeside Community Bank",
        "console": "Customer Service Workstation",
        "version": "MSC 7.4.0",
        "accent": "#7a2e2e",
        "nav_home": "Start",
        "nav_inq": "Customer Lookup",
        "nav_open": "New Sub-Account",
        "nav_reports": "Reporting",
        "member_label": "Cust. #",
        "search_btn": "Find",
        "cur_label": "Ledger Balance",
        "avail_label": "Avail Balance",
        "products": {
            "SAV": "Regular Savings",
            "CHK": "Everyday Checking",
            "MMK": "Premier Money Market",
            "CD": "Term Certificate",
        },
    },
}

# Product codes a new sub-account can be opened with (order = dropdown order).
OPENABLE = ["SAV", "MMK"]
