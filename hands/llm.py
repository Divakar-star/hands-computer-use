"""Model adapters. The agent needs exactly one thing from a model: given a screen
description and a tool list, return ONE tool call (with a rationale). Providers
differ only in wire format, so the interface is tiny and swappable by price.

  AnthropicClient  - Messages API, tool_choice=any
  OpenAIClient     - Chat Completions, tool_choice=required
  ScriptedModel    - deterministic stand-in for tests / offline runs (NOT a discovery run;
                     evidence from it is labelled as scripted)
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from .observation import Observation


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass
class ModelTurn:
    call: ToolCall | None
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class ModelClient(Protocol):
    name: str

    def decide(self, system: str, user: str, tools: list[ToolSpec], *,
               image_png: bytes | None = None, obs: Observation | None = None) -> ModelTurn: ...


# USD per 1M tokens (input, output), from the providers' model pages. For a cost readout only.
PRICES = {"gpt-5.6-luna": (0.20, 1.20)}


def estimate_cost(model: str, usage: dict[str, int]) -> str:
    price = PRICES.get(model)
    if not price or not usage:
        return ""
    usd = usage.get("in", 0) / 1e6 * price[0] + usage.get("out", 0) / 1e6 * price[1]
    return f"~${usd:.4f} at ${price[0]}/${price[1]} per 1M in/out tokens"


class AnthropicClient:
    def __init__(self, model: str | None = None, max_tokens: int = 1500):
        import anthropic
        self.name = model or os.environ.get("HANDS_MODEL", "claude-sonnet-5")
        self.max_tokens = max_tokens
        self._c = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY from the environment

    def decide(self, system, user, tools, *, image_png=None, obs=None) -> ModelTurn:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image_png:
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": base64.b64encode(image_png).decode()}})
        resp = self._c.messages.create(
            model=self.name, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
            tools=[{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools],
            tool_choice={"type": "any"})
        call, text = None, ""
        for block in resp.content:
            if block.type == "tool_use" and call is None:
                call = ToolCall(block.name, dict(block.input), block.id)
            elif block.type == "text":
                text += block.text
        return ModelTurn(call, text, {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens})


class OpenAIClient:
    def __init__(self, model: str | None = None):
        import openai
        # gpt-5.6-luna: cost-tier model, supports Chat Completions + function calling + image input.
        self.name = model or os.environ.get("HANDS_MODEL", "gpt-5.6-luna")
        # Verified against the live API: on /v1/chat/completions, gpt-5.6-luna rejects function tools
        # combined with any reasoning effort other than "none" (HTTP 400). So this adapter defaults to
        # "none"; a reasoning-enabled run would need the Responses API (not implemented).
        self.reasoning_effort = os.environ.get("HANDS_REASONING_EFFORT") or "none"
        if self.reasoning_effort != "none":
            raise ValueError(
                f"HANDS_REASONING_EFFORT={self.reasoning_effort!r}: Chat Completions with function tools only "
                "supports 'none' for gpt-5.6-luna (the API returns HTTP 400 otherwise).")
        # Cap per call (reasoning tokens count toward it). Stops one runaway response from costing much.
        self.max_output_tokens = int(os.environ.get("HANDS_MAX_OUTPUT_TOKENS", "3000"))
        self._c = openai.OpenAI()                # reads OPENAI_API_KEY from the environment

    def decide(self, system, user, tools, *, image_png=None, obs=None) -> ModelTurn:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image_png:
            url = "data:image/png;base64," + base64.b64encode(image_png).decode()
            content.append({"type": "image_url", "image_url": {"url": url}})
        extra: dict[str, Any] = {"reasoning_effort": self.reasoning_effort}
        resp = self._c.chat.completions.create(
            model=self.name,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            tools=[{"type": "function", "function": {"name": t.name, "description": t.description,
                                                     "parameters": t.schema}} for t in tools],
            tool_choice="required", max_completion_tokens=self.max_output_tokens, **extra)
        msg = resp.choices[0].message
        call = None
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            try:
                call = ToolCall(tc.function.name, json.loads(tc.function.arguments or "{}"), tc.id)
            except json.JSONDecodeError:
                call = None                      # malformed arguments = a bad turn, handled by the agent loop
        u = resp.usage
        return ModelTurn(call, msg.content or "", {"in": u.prompt_tokens, "out": u.completion_tokens} if u else {})


class ScriptedModel:
    """Follows a fixed script, resolving controls by (role, name) against the *current*
    observation so it still works when refs shift - a deterministic, key-free 'model'."""
    name = "scripted"

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.i = 0

    def decide(self, system, user, tools, *, image_png=None, obs=None) -> ModelTurn:
        if self.i >= len(self.script):
            return ModelTurn(ToolCall("finish", {"success": False, "summary": "script exhausted",
                                                 "rationale": "no more scripted actions"}))
        step = dict(self.script[self.i])
        self.i += 1
        find = step.pop("find", None)
        if find is not None:
            assert obs is not None, "ScriptedModel needs the observation"
            hits = [e for e in obs.elements if e.role == find["role"]
                    and (e.name or e.text).strip().casefold() == find["name"].strip().casefold()
                    and (find.get("group") is None or e.group.casefold() == find["group"].casefold())]
            if not hits:
                raise LookupError(f"scripted step {self.i}: no control {find} on screen")
            step["ref"] = hits[0].ref
        tool = step.pop("tool")
        step.setdefault("rationale", f"scripted {tool}")
        return ModelTurn(ToolCall(tool, step), usage={})
