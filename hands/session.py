"""Session establishment lives OUTSIDE the recorded capability.

Credentials are a precondition supplied by the harness, not a step the model
performs or an artifact records: the model never sees the password, the artifact
never contains it, and re-authentication (session expiry) is a deliberate,
bounded recovery instead of a replayed login flow. In production this is the
seam for a credential vault + SSO/MFA broker; here it reads environment vars.
"""
from __future__ import annotations

import os
from typing import Protocol

from .redact import Redactor


class SessionProvider(Protocol):
    def ensure(self, surface) -> None: ...


class MscSession:
    def __init__(self, base_url: str, redactor: Redactor, user: str | None = None,
                 password: str | None = None, entry: str = "/msc/main"):
        self.base = base_url.rstrip("/")
        self.entry = entry
        self.user = user or os.environ.get("MSC_USER", "teller01")
        self.password = password or os.environ.get("MSC_PASS", "demo-not-a-real-password")
        redactor.register(self.password, "[SECRET]")

    def ensure(self, surface) -> None:
        """Land on the app; sign on if we were bounced to the login page. Uses the raw page
        (harness code, not a model action) but the network allowlist still applies."""
        page = surface.page
        page.goto(self.base + self.entry, wait_until="load")
        if "/msc/login" in page.url:
            page.fill("input[name=USRID]", self.user)
            page.fill("input[name=PWD]", self.password)
            page.click("input[type=submit]")
            page.wait_for_load_state("load")
            page.goto(self.base + self.entry, wait_until="load")
        surface.settle()
        if "/msc/login" in page.url:
            raise RuntimeError("sign-on failed: still on the login page")
