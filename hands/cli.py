"""Command line: discover | replay | catalog | schema."""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _kv(items: list[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if not _:
            raise SystemExit(f"expected key=value, got {it!r}")
        out[k] = v
    return out


def _coerce(v: str):
    return {"true": True, "false": False}.get(v.lower(), v)


@contextlib.contextmanager
def _maybe_mock(args):
    """--spawn-mock starts the bundled mock console in-process so the demo needs no second terminal."""
    if not getattr(args, "spawn_mock", False):
        yield
        return
    from urllib.parse import urlparse

    from .harness import MockServer
    u = urlparse(args.target)
    with MockServer(tenant=args.mock_tenant, port=u.port or 8765, faults=args.mock_faults):
        print(f"[mock] {args.mock_tenant} console at {args.target} faults={args.mock_faults or 'none'}")
        yield


def _operator(kind: str):
    if kind == "cli":
        from .control import CliOperator
        return CliOperator()
    if kind == "http":
        from .control import HttpOperator
        return HttpOperator()
    return None


def _model(args):
    from . import scripts
    if args.provider == "anthropic":
        from .llm import AnthropicClient
        return AnthropicClient(args.model)
    if args.provider == "openai":
        from .llm import OpenAIClient
        return OpenAIClient(args.model)
    if args.provider == "scripted-savings":
        return scripts.savings_balance_script()
    if args.provider == "scripted-open":
        return scripts.open_review_script()
    raise SystemExit(f"unknown provider {args.provider}")


def cmd_discover(args) -> int:
    from .pipeline import discover
    from .policy import load_policy
    model = _model(args)
    if args.provider.startswith("scripted"):
        print("NOTE: scripted model - a key-free dry run, NOT the real LLM discovery run.")
    with _maybe_mock(args):
        cap, res, ver = discover(
            target=args.target, goal=args.goal, model=model, policy=load_policy(args.policy),
            profile_path=args.profile, out_dir=args.out, cap_dir=args.caps, headless=not args.headed,
            tenant=args.mock_tenant if args.spawn_mock else None, cap_id=args.id, masks=_kv(args.mask),
            max_steps=args.max_steps, token_budget=args.token_budget, vision=args.vision, operator=_operator(args.escalate),
            on_stuck="escalate" if args.escalate != "none" else "fail", verify=not args.no_verify)
    return 0 if cap is not None and (ver is None or ver.status == "success") else 1


def cmd_replay(args) -> int:
    from .pipeline import replay
    from .policy import load_policy
    from .recorder import load_capability
    from .tenancy import load_override
    cap = load_capability(args.capability)
    inputs = {k: _coerce(v) for k, v in _kv(args.input).items()}
    ov = load_override(args.override) if args.override else None
    with _maybe_mock(args):
        res = replay(cap, inputs, target=args.target, policy=load_policy(args.policy), out_dir=args.out,
                     headless=not args.headed, override=ov, approvals=set(args.approve or []),
                     operator=_operator(args.escalate), on_stuck="escalate" if args.escalate != "none" else "fail",
                     shots="steps" if args.shots else "failure", masks=_kv(args.mask))
    print(json.dumps(res.model_dump(mode="json", exclude_none=True), indent=2))
    return {"success": 0, "business_outcome": 0}.get(res.status, 2)


def cmd_catalog(args) -> int:
    from .recorder import load_capability
    specs = [load_capability(p).to_tool_spec() for p in sorted(Path(args.dir).glob("*.json"))]
    print(json.dumps(specs, indent=2))
    return 0


def cmd_models(args) -> int:
    """List the model IDs your key can use, so HANDS_MODEL is an exact, valid string."""
    if args.provider == "openai":
        import openai
        ids = sorted(m.id for m in openai.OpenAI().models.list())
    else:
        import anthropic
        ids = sorted(m.id for m in anthropic.Anthropic().models.list())
    needle = (args.filter or "").lower()
    shown = [i for i in ids if needle in i.lower()]
    print("\n".join(shown) or f"(no model ids contain {args.filter!r}; {len(ids)} available in total)")
    return 0


def cmd_schema(args) -> int:
    from .schema import Capability, Result
    doc = {"Capability": Capability.model_json_schema(), "Result": Result.model_json_schema()}
    Path(args.out).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .envfile import load_env
    load_env()                      # API keys from a git-ignored .env, if present
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="hands", description="Computer-use capability recorder / replayer")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--target", default="http://127.0.0.1:8765", help="base URL of the application")
        sp.add_argument("--policy", default=str(ROOT / "policies" / "msc.policy.json"))
        sp.add_argument("--out", default=str(ROOT / "evidence"), help="where run evidence is written")
        sp.add_argument("--headed", action="store_true", help="show the browser (required for a human to take over)")
        sp.add_argument("--escalate", choices=["none", "cli", "http"], default="none",
                        help="how to reach a human when stuck / when approval is needed")
        sp.add_argument("--mask", action="append", metavar="name=value", help="extra value to scrub from all logs")
        sp.add_argument("--spawn-mock", action="store_true", help="run the bundled mock console for this command")
        sp.add_argument("--mock-tenant", default="prairie", choices=["prairie", "lakeside"])
        sp.add_argument("--mock-faults", default="", help="e.g. notice,slow_ms=2500,flaky_member=1")

    d = sub.add_parser("discover", help="LLM-driven run against a live UI; records + verifies a capability")
    common(d)
    d.add_argument("--goal", required=True)
    d.add_argument("--provider", default="anthropic", choices=["anthropic", "openai", "scripted-savings", "scripted-open"])
    d.add_argument("--model", default=None, help="model id (default: $HANDS_MODEL or provider default)")
    d.add_argument("--caps", default=str(ROOT / "capabilities"))
    d.add_argument("--profile", default=str(ROOT / "profiles" / "msc.json"))
    d.add_argument("--id", default=None, help="override the capability slug")
    d.add_argument("--max-steps", type=int, default=15, help="stop after this many model calls (default 15)")
    d.add_argument("--token-budget", type=int, default=60000, help="hard cap on input+output tokens for the run (default 60000)")
    d.add_argument("--vision", action="store_true", help="also send a (masked) screenshot each step")
    d.add_argument("--no-verify", action="store_true")
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of a saved capability (no LLM)")
    common(r)
    r.add_argument("capability")
    r.add_argument("--input", action="append", metavar="name=value")
    r.add_argument("--override", help="tenant override file")
    r.add_argument("--approve", action="append", metavar="STEP_ID", help="pre-approve an irreversible step ('*' = all)")
    r.add_argument("--shots", action="store_true", help="screenshot after every step (default: on failure only)")
    r.set_defaults(fn=cmd_replay)

    c = sub.add_parser("catalog", help="list saved capabilities as agent-callable tool specs")
    c.add_argument("--dir", default=str(ROOT / "capabilities"))
    c.set_defaults(fn=cmd_catalog)

    m = sub.add_parser("models", help="list the model ids your API key can use (needs the key)")
    m.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    m.add_argument("--filter", default="", help="only ids containing this text, e.g. luna or gpt-5")
    m.set_defaults(fn=cmd_models)

    s = sub.add_parser("schema", help="export the JSON Schema of the artifact + result contract")
    s.add_argument("--out", default=str(ROOT / "docs" / "capability.schema.json"))
    s.set_defaults(fn=cmd_schema)

    args = p.parse_args(argv)
    if args.cmd == "schema":
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    return args.fn(args)

