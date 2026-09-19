"""Pure logic over Observations: locator resolution, condition evaluation, extraction.

No browser here on purpose. Replay resolves a Target against the same
descriptors discovery saw, so (a) recording can prove each locator is unique
before saving it, (b) all of this is unit-testable, and (c) a desktop surface
reuses it unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .observation import Element, Observation
from .schema import Condition, Locator, Target, ValueType

_PARAM = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def subst(template: str, params: dict[str, str]) -> str:
    return _PARAM.sub(lambda m: str(params.get(m.group(1), m.group(0))), template)


def glob_to_regex(pattern: str, params: dict[str, str]) -> re.Pattern[str]:
    parts = re.split(r"(\*)", subst(pattern, params))
    return re.compile("^" + "".join(".*" if p == "*" else re.escape(p) for p in parts) + "$")


def _strip_origin(url: str) -> str:
    return re.sub(r"^https?://[^/]+", "", url or "")


def _url_path(url: str) -> str:
    """Path only: a checkpoint is about which screen we are on, not its query string."""
    return _strip_origin(url).split("?", 1)[0].split("#", 1)[0]


# ------------------------------------------------------------------ locators
def locator_matches(loc: Locator, el: Element, params: dict[str, str]) -> bool:
    if loc.kind == "field_name":
        if not el.name_attr or el.name_attr != loc.value or (loc.role is not None and el.role != loc.role):
            return False
        # radio groups share one field name; the option's own label tells them apart
        return loc.name is None or norm(el.name) == norm(loc.name)
    if loc.kind == "href":
        return bool(el.href) and bool(glob_to_regex(loc.value or "", params).match(_strip_origin(el.href)))
    if loc.kind == "role_name":
        if el.role != loc.role or norm(el.name or el.text) != norm(loc.name):
            return False
        return loc.group is None or norm(el.group) == norm(loc.group)
    if loc.kind == "visible_text":
        return norm(el.text or el.name) == norm(loc.name) and (loc.role is None or el.role == loc.role)
    if loc.kind == "structural":
        return el.role == loc.role and el.ordinal == loc.ordinal
    return False


@dataclass
class Resolution:
    status: str                      # ok | none | ambiguous
    element: Element | None = None
    used: Locator | None = None
    tried: list[tuple[str, int]] = field(default_factory=list)   # (locator kind, match count)
    warnings: list[str] = field(default_factory=list)            # (kind, detail) drift signals
    drift: list[tuple[str, str]] = field(default_factory=list)


def resolve(target: Target, obs: Observation, params: dict[str, str]) -> Resolution:
    """Strategy cascade with cross-checking.

    Every locator is evaluated. The first that yields exactly one element wins,
    but the others are still consulted: if a *lower* strategy points at a
    different element, that disagreement is a drift signal surfaced to the
    caller. If the top strategies fail and a fallback succeeds, that is reported
    as a locator_fallback - the flow still works, and the reviewer is told why.
    """
    frames = [target.frame] if target.frame is not None else [None]
    res = Resolution(status="none")
    for hint_pass, fr in enumerate(frames + ([None] if target.frame is not None else [])):
        pool = [e for e in obs.elements if fr is None or e.frame == fr]
        matches_by_loc: list[tuple[Locator, list[Element]]] = []
        for loc in target.locators:
            ms = [e for e in pool if locator_matches(loc, e, params)]
            matches_by_loc.append((loc, ms))
        res.tried = [(loc.kind, len(ms)) for loc, ms in matches_by_loc]
        unique = [(loc, ms[0]) for loc, ms in matches_by_loc if len(ms) == 1]
        if unique:
            res.used, res.element = unique[0]
            res.status = "ok"
            if hint_pass == 1:
                res.drift.append(("frame_changed", f"control not in frame {target.frame!r}; found in {res.element.frame!r}"))
            first_loc = target.locators[0]
            if res.used is not first_loc:
                res.drift.append(("locator_fallback",
                                  f"primary '{first_loc.kind}' matched {_count(res.tried, first_loc.kind)} "
                                  f"element(s); used '{res.used.kind}'"))
            for loc, el in unique[1:]:
                if el.ref != res.element.ref:
                    res.drift.append(("strategy_disagreement",
                                      f"'{res.used.kind}' and '{loc.kind}' resolve to different controls"))
            return res
        if any(len(ms) > 1 for _, ms in matches_by_loc):
            res.status = "ambiguous"
    return res


def _count(tried: list[tuple[str, int]], kind: str) -> int:
    return next((n for k, n in tried if k == kind), 0)


# ------------------------------------------------------------------ conditions
def eval_condition(c: Condition, obs: Observation, params: dict[str, str]) -> bool:
    k = c.kind
    if k == "all":
        return all(eval_condition(x, obs, params) for x in c.of)
    if k == "any":
        return any(eval_condition(x, obs, params) for x in c.of)
    if k == "not":
        return not any(eval_condition(x, obs, params) for x in c.of)
    if k == "text_contains":
        return norm(subst(c.value or "", params)) in norm(obs.text(c.frame))
    if k == "text_regex":
        return re.search(subst(c.value or "", params), obs.text(c.frame), re.I | re.S) is not None
    if k == "frame_url":
        rx = glob_to_regex(c.value or "", params)
        if c.frame is None:
            return any(rx.match(_url_path(f.url)) for f in obs.frames)
        f = obs.frame(c.frame)
        return bool(f and rx.match(_url_path(f.url)))
    if k == "status_in":
        fs = obs.frames if c.frame is None else [f for f in obs.frames if f.name == c.frame]
        return any(f.status in (c.statuses or []) for f in fs)
    if k == "element_present":
        return c.target is not None and resolve(c.target, obs, params).status != "none"
    raise ValueError(f"unknown condition kind {k}")


def describe_condition(c: Condition, params: dict[str, str] | None = None) -> str:
    params = params or {}
    if c.describe:
        return c.describe
    if c.kind in ("all", "any", "not"):
        return f"{c.kind}(" + ", ".join(describe_condition(x, params) for x in c.of) + ")"
    if c.kind == "status_in":
        return f"http status in {c.statuses}" + (f" in frame {c.frame}" if c.frame else "")
    if c.kind == "element_present":
        return f"control present: {c.target.description if c.target else '?'}"
    where = f" in frame {c.frame}" if c.frame else ""
    return f"{c.kind} {subst(c.value or '', params)!r}{where}"


# ------------------------------------------------------------------ extraction
def read_labeled_value(obs: Observation, labels: list[str], frame: str | None) -> str | None:
    wanted = {norm(l) for l in labels}
    for f in obs.frames:
        if frame is not None and f.name != frame:
            continue
        for label, value in f.fields:
            if norm(label.rstrip(":")) in wanted:
                return value
    return None


def coerce(raw: str, vtype: ValueType) -> str | int | bool:
    s = raw.strip()
    if vtype == ValueType.string or vtype == ValueType.date:
        return s
    if vtype == ValueType.boolean:
        return norm(s) in ("yes", "true", "1", "y", "active")
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.startswith("$-")
    digits = re.sub(r"[^\d.]", "", s)
    if digits == "":
        raise ValueError(f"cannot read a number from {raw!r}")
    try:
        d = Decimal(digits)
    except InvalidOperation as exc:  # pragma: no cover
        raise ValueError(f"cannot read a number from {raw!r}") from exc
    d = -d if neg else d
    if vtype == ValueType.integer:
        return int(d)
    return format(d, "f")   # decimal carried as string: no float error on money
