"""Live human-handoff demo you can do by hand.

Replays an artifact whose last step is the IRREVERSIBLE 'Confirm and Submit' (declared "safe" in the
file - the runtime overrides that). Automation runs to the review screen, then STOPS and raises an
approval request. You then:

  * open the operator page (http://127.0.0.1:8770) and 'Take control' - the browser window is the SAME
    live session; look at it, click around if you like (your actions are recorded, values never);
  * hand back with 'Retry the step' (= approve; automation performs the commit),
    'I completed the step' (you did it yourself) or 'Abort the run' (deny).

    python scripts/demo_handoff.py            # web operator page
    python scripts/demo_handoff.py --cli      # terminal operator instead
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hands.control import CliOperator, HttpOperator  # noqa: E402
from hands.harness import MockServer, with_commit_step  # noqa: E402
from hands.pipeline import replay  # noqa: E402
from hands.policy import load_policy  # noqa: E402
from hands.recorder import load_capability  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", action="store_true")
    ap.add_argument("--cap", default=str(ROOT / "capabilities" / "msc.open_subaccount_to_review.json"))
    args = ap.parse_args()
    cap = with_commit_step(load_capability(args.cap))
    inputs = {"member_number": "12345", "account_type": "SAV", "initial_deposit": "25.00", "nickname": "Demo",
              "member_consent_and_disclosures_provided": True}
    with MockServer(port=8765) as srv:
        res = replay(cap, inputs, target=srv.url, policy=load_policy(ROOT / "policies" / "msc.policy.json"),
                     out_dir=ROOT / "evidence" / "_handoff-demo", headless=False, label="handoff-demo",
                     operator=CliOperator() if args.cli else HttpOperator())
        print(json.dumps(res.model_dump(mode="json", exclude_none=True), indent=2))
        print("commits the app actually executed:", srv.commits)


if __name__ == "__main__":
    main()
