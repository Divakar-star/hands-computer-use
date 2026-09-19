"""What the surface reports: a technology-neutral snapshot of the current screen.

This is the seam between "how we perceive a surface" and "the recorded flow".
A web surface fills it from the DOM (across frames); a desktop surface would
fill the same shapes from an accessibility tree (UIA / AX) - `frame` becomes the
window/pane, `role`/`name` come from the AX node, `attrs` carry automation ids.
Nothing above this file knows about HTML, and the artifact never stores raw
selectors, only descriptors of the fields below.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Element:
    ref: int
    frame: str
    tag: str
    role: str
    name: str = ""            # accessible name (incl. adjacent-cell label in legacy tables)
    group: str = ""           # row/group label, e.g. "Funding Source" for a radio button
    type: str = ""
    name_attr: str = ""       # machine name (form field name)
    href: str = ""
    form_action: str = ""
    value: str = ""
    checked: bool = False
    disabled: bool = False
    text: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    ordinal: int = 0          # index among same-role elements in its frame

    def brief(self) -> str:
        bits = [f"[{self.ref}]", self.role, f'"{self.name or self.text}"']
        if self.group and self.group != self.name:
            bits.append(f"(group: {self.group})")
        if self.role == "textbox":
            bits.append(f"value={self.value!r}" if self.value else "empty")
        if self.role in ("checkbox", "radio"):
            bits.append("checked" if self.checked else "unchecked")
        if self.role == "combobox":
            bits.append("options=" + "|".join(o["text"] for o in self.options[:8]))
        if self.href and not self.href.startswith("javascript"):
            bits.append(f"href={self.href}")
        if self.disabled:
            bits.append("disabled")
        if self.frame:
            bits.append(f"frame={self.frame}")
        return " ".join(bits)


@dataclass
class FrameState:
    name: str
    url: str
    status: int | None = None
    title: str = ""
    text: str = ""
    fields: list[tuple[str, str]] = field(default_factory=list)   # (label, value) cell pairs


@dataclass
class Observation:
    frames: list[FrameState]
    elements: list[Element]
    dialog: str | None = None       # last unhandled JS dialog text, if any

    def frame(self, name: str | None) -> FrameState | None:
        if name is None:
            return None
        return next((f for f in self.frames if f.name == name), None)

    def text(self, frame: str | None = None) -> str:
        fs = [f for f in self.frames if frame is None or f.name == frame]
        return "\n".join(f.text for f in fs)

    def all_fields(self) -> list[tuple[str, str]]:
        return [p for f in self.frames for p in f.fields]

    def signature(self) -> str:
        """Cheap identity for no-progress detection."""
        return "|".join(f"{f.name}:{f.url}:{len(f.text)}" for f in self.frames) + f"#{len(self.elements)}"

    def to_dict(self) -> dict[str, Any]:
        return {"frames": [asdict(f) for f in self.frames],
                "elements": [asdict(e) for e in self.elements], "dialog": self.dialog}
