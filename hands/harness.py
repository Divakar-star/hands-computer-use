"""Test/demo harness: run the mock console in-process and flip its fault switches.
Nothing here is used by the production replay path."""
from __future__ import annotations

import json
import threading
import urllib.request
from typing import Any

from werkzeug.serving import make_server

from mockbank import create_app
from mockbank.faults import Faults, parse_spec


class MockServer:
    def __init__(self, tenant: str = "prairie", port: int = 8765, faults: str = ""):
        self.app = create_app(tenant, parse_spec(faults))
        self.port = port
        self._srv = make_server("127.0.0.1", port, self.app, threaded=True)
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self):
        return self.app.config["MSC_STATE"]

    def __enter__(self) -> "MockServer":
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()

    # -- admin ---------------------------------------------------------------------------
    def _post(self, path: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(self.url + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def set_faults(self, **faults: Any) -> None:
        self._post("/__admin/faults", faults)

    def reset(self) -> None:
        self._post("/__admin/reset")

    def expire_sessions(self) -> None:
        self._post("/__admin/expire_sessions")

    @property
    def commits(self) -> list[dict]:
        return list(self.state.commits)


_ = Faults


def with_commit_step(cap, declared_risk: str = "safe"):
    """Test/demo helper: an artifact that appends the irreversible 'Confirm and Submit' click while
    UNDERSTATING its risk - proves the runtime re-derives risk instead of trusting the file."""
    from hands.schema import Capability
    d = cap.model_dump(mode="json")
    d["steps"].append({
        "id": "s99", "intent": "Confirm and submit the new sub-account", "action": "click", "risk": declared_risk,
        "target": {"description": "button 'Confirm and Submit'", "frame": "body", "role": "button",
                   "locators": [{"kind": "role_name", "role": "button", "name": "Confirm and Submit"}]},
        "expect": {"kind": "frame_url", "frame": "body", "value": "/msc/open/commit"}})
    d["success"] = {"kind": "text_contains", "value": "Sub-Account Opened"}
    d["digest"] = None
    return Capability.model_validate(d)
