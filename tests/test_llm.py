"""Offline tests of the provider adapters against stub SDK clients: request shape (tool schemas,
forced tool use) and response parsing. Catches wiring mistakes without spending a token."""
import json
from types import SimpleNamespace

from hands.agent import TOOLS
from hands.envfile import load_env
from hands.llm import AnthropicClient, OpenAIClient


def test_openai_adapter_request_and_parse():
    seen = {}

    def create(**kw):
        seen.update(kw)
        fn = SimpleNamespace(name="click", arguments=json.dumps({"ref": 3, "rationale": "open it"}))
        msg = SimpleNamespace(tool_calls=[SimpleNamespace(id="c1", function=fn)], content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5))

    client = OpenAIClient.__new__(OpenAIClient)
    client.name, client.reasoning_effort, client.max_output_tokens = "test-model", "none", 3000
    client._c = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.decide("system text", "user text", TOOLS, image_png=b"\x89PNG")
    assert turn.call.name == "click" and turn.call.args == {"ref": 3, "rationale": "open it"}
    assert turn.usage == {"in": 11, "out": 5}
    assert seen["tool_choice"] == "required" and seen["model"] == "test-model"
    assert seen["max_completion_tokens"] == 3000          # per-call cap: a runaway response cannot be expensive
    assert {t["function"]["name"] for t in seen["tools"]} == {t.name for t in TOOLS}
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in seen["tools"])
    parts = seen["messages"][1]["content"]
    assert parts[0]["type"] == "text" and parts[1]["type"] == "image_url"      # vision optional, attached when given


def test_openai_defaults_to_luna_with_reasoning_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("HANDS_MODEL", raising=False)
    monkeypatch.delenv("HANDS_REASONING_EFFORT", raising=False)
    c = OpenAIClient()
    assert c.name == "gpt-5.6-luna" and c.reasoning_effort == "none"
    seen = {}
    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content="x"))], usage=None)
    c._c = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    c.decide("s", "u", TOOLS)
    assert seen["reasoning_effort"] == "none"      # the only value the live API accepts alongside function tools


def test_openai_rejects_an_unsupported_reasoning_effort_before_calling_the_api(monkeypatch):
    import pytest
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("HANDS_REASONING_EFFORT", "low")
    with pytest.raises(ValueError, match="only supports 'none'"):
        OpenAIClient()


def test_openai_malformed_tool_arguments_are_a_bad_turn_not_a_crash():
    fn = SimpleNamespace(name="click", arguments="{not json")
    msg = SimpleNamespace(tool_calls=[SimpleNamespace(id="c", function=fn)], content=None)
    client = OpenAIClient.__new__(OpenAIClient)
    client.name, client.reasoning_effort, client.max_output_tokens = "m", "none", 3000
    client._c = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None))))
    assert client.decide("s", "u", TOOLS).call is None


def test_openai_adapter_tolerates_no_tool_call():
    msg = SimpleNamespace(tool_calls=None, content="I refuse")
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
    client = OpenAIClient.__new__(OpenAIClient)
    client.name, client.reasoning_effort, client.max_output_tokens = "m", "none", 3000
    client._c = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp)))
    turn = client.decide("s", "u", TOOLS)
    assert turn.call is None and turn.text == "I refuse"      # the agent loop treats this as a bad turn, not a crash


def test_anthropic_adapter_request_and_parse():
    seen = {}

    def create(**kw):
        seen.update(kw)
        block = SimpleNamespace(type="tool_use", name="extract_value", id="t1",
                                input={"name": "bal", "label": "Available Balance", "type": "decimal",
                                       "description": "d", "rationale": "r"})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok"), block],
                               usage=SimpleNamespace(input_tokens=20, output_tokens=7))

    client = AnthropicClient.__new__(AnthropicClient)
    client.name, client.max_tokens = "test-model", 100
    client._c = SimpleNamespace(messages=SimpleNamespace(create=create))
    turn = client.decide("sys", "usr", TOOLS)
    assert turn.call.name == "extract_value" and turn.usage == {"in": 20, "out": 7}
    assert seen["tool_choice"] == {"type": "any"}
    assert all({"name", "description", "input_schema"} <= set(t) for t in seen["tools"])


def test_env_loader_sets_only_missing_names_and_never_returns_values(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nOPENAI_API_KEY=sk-secret\nexport HANDS_MODEL='my-model'\nALREADY=new\n\nbad line\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HANDS_MODEL", raising=False)
    monkeypatch.setenv("ALREADY", "keep")
    names = load_env(f)
    assert sorted(names) == ["HANDS_MODEL", "OPENAI_API_KEY"]
    import os
    assert os.environ["OPENAI_API_KEY"] == "sk-secret" and os.environ["HANDS_MODEL"] == "my-model"
    assert os.environ["ALREADY"] == "keep"
    assert "sk-secret" not in "".join(names)
    monkeypatch.delenv("OPENAI_API_KEY"); monkeypatch.delenv("HANDS_MODEL")


def test_cost_estimate_uses_the_documented_luna_prices():
    from hands.llm import estimate_cost
    assert "$0.0014" in estimate_cost("gpt-5.6-luna", {"in": 2000, "out": 800})     # 2000*0.2/1e6 + 800*1.2/1e6
    assert estimate_cost("unknown-model", {"in": 1, "out": 1}) == ""
