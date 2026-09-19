"""The perceive/act boundary. `Surface` is the interface everything else uses;
`PlaywrightSurface` is the concrete web implementation.

Why not just CSS selectors: the target class is legacy web (framesets, nested
tables, no ids/test-ids). So perception is *semantic*: for every interactive
control we compute role + accessible name (including the label in the adjacent
table cell, which is how these apps label inputs), the machine `name`
attribute, href / form action, and an ordinal. Those descriptors - not
selectors - are what the artifact stores and what replay resolves against.
Acting on a resolved control uses a throwaway `data-hands-ref` attribute stamped
during the same observation.
"""
from __future__ import annotations

import time
from typing import Callable, Protocol

from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright, sync_playwright

from .observation import Element, FrameState, Observation
from .policy import Policy, PolicyViolation
from .redact import Redactor
from .schema import ActionType


class Surface(Protocol):
    def observe(self) -> Observation: ...
    def perform(self, action: ActionType, el: Element | None, value: str | bool | None = None) -> None: ...
    def goto(self, url: str) -> None: ...
    def reload_frame(self, name: str) -> None: ...
    def screenshot(self, masked: bool = True) -> bytes: ...
    def snapshot_text(self) -> str: ...
    def wait(self, ms: int) -> None: ...


# --------------------------------------------------------------------------- page-side scripts
_JS_COLLECT = r"""
(base) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-hands-ref]').forEach(e => e.removeAttribute('data-hands-ref'));
  const vis = el => {
    if (!el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const roleOf = el => {
    const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const ty = (el.type || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      return 'textbox';
    }
    return 'generic';
  };
  const followingText = el => {
    let n = el.nextSibling, out = '';
    while (n) {
      if (n.nodeType === 3) out += n.textContent;
      else if (n.nodeType === 1) {
        if (['INPUT', 'SELECT', 'TEXTAREA', 'BR', 'BUTTON'].includes(n.tagName)) break;
        out += n.textContent;
      }
      n = n.nextSibling;
    }
    return norm(out);
  };
  // legacy layouts label a control with the nearest cell to its left in the same row
  const cellLabel = el => {
    const td = el.closest('td,th'); if (!td) return '';
    let prev = td.previousElementSibling;
    while (prev) {
      const t = norm(prev.innerText);
      if (t && !prev.querySelector('input,select,textarea,a,button')) return t.replace(/:$/, '');
      prev = prev.previousElementSibling;
    }
    return '';
  };
  const nameOf = el => {
    const aria = el.getAttribute('aria-label'); if (aria) return norm(aria);
    const t = el.tagName.toLowerCase(), ty = (el.type || '').toLowerCase();
    if (t === 'input' && ['submit', 'button', 'reset'].includes(ty)) return norm(el.value);
    if (t === 'a' || t === 'button' || el.getAttribute('onclick')) return norm(el.innerText);
    if (el.labels && el.labels.length) return norm(el.labels[0].innerText);
    if (ty === 'checkbox' || ty === 'radio') { const f = followingText(el); if (f) return f; }
    return cellLabel(el) || norm(el.getAttribute('placeholder') || el.getAttribute('title') || '');
  };
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [onclick], [role=button], [role=link], [role=checkbox]';
  const seen = new Set(), ordinals = {}, elements = [];
  document.querySelectorAll(sel).forEach(el => {
    if (seen.has(el) || !vis(el)) return; seen.add(el);
    const role = roleOf(el), i = base + elements.length;
    ordinals[role] = (ordinals[role] ?? -1) + 1;
    el.setAttribute('data-hands-ref', String(i));
    const form = el.closest('form');
    let action = '';
    if (form) { try { action = new URL(form.action, location.href).pathname; } catch (e) {} }
    const ty = (el.type || '').toLowerCase();
    elements.push({
      ref: i, tag: el.tagName.toLowerCase(), role, name: nameOf(el),
      group: (ty === 'radio' || ty === 'checkbox') ? cellLabel(el) : '',
      type: ty, name_attr: el.getAttribute('name') || '',
      href: el.getAttribute('href') || '', form_action: action,
      value: ty === 'password' ? '' : (el.value ?? ''),
      checked: !!el.checked, disabled: !!el.disabled,
      text: norm(el.innerText || ''),
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => ({value: o.value, text: norm(o.text)})) : [],
      ordinal: ordinals[role],
    });
  });
  const fields = [];
  document.querySelectorAll('tr').forEach(tr => {
    const cells = Array.from(tr.children).filter(c => /^(TD|TH)$/.test(c.tagName));
    if (cells.length !== 2) return;
    const l = norm(cells[0].innerText), v = norm(cells[1].innerText);
    if (l && v && l.length <= 40 && !cells[0].querySelector('table,input,select,a'))
      fields.push([l.replace(/:$/, ''), v]);
  });
  return {
    url: location.href, title: document.title,
    text: document.body ? (document.body.innerText || '').slice(0, 8000) : '',
    hasFrames: !!document.querySelector('frameset'), elements, fields,
  };
}
"""

_JS_MASK = r"""
(args) => {
  const {masks, patterns} = args;
  const undo = [];
  const block = s => '█'.repeat(Math.min(Math.max(s.length, 3), 12));
  const rxs = patterns.map(p => new RegExp(p, 'g'));
  const w = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  const nodes = []; while (w.nextNode()) nodes.push(w.currentNode);
  const scrub = t => {
    for (const m of masks) if (m && t.includes(m)) t = t.split(m).join(block(m));
    for (const rx of rxs) t = t.replace(rx, x => block(x));
    return t;
  };
  for (const n of nodes) { const o = n.textContent, t = scrub(o); if (t !== o) { undo.push(() => n.textContent = o); n.textContent = t; } }
  document.querySelectorAll('input,textarea').forEach(i => {
    const o = i.value;
    const t = i.type === 'password' ? '•'.repeat(8) : scrub(o);
    if (t !== o) { undo.push(() => { i.value = o; }); i.value = t; }
  });
  window.__handsUndo = () => undo.forEach(f => f());
}
"""

_INIT_HUMAN = r"""
(() => {
  if (window.__handsHumanInstalled) return; window.__handsHumanInstalled = true;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const send = o => { try { window.__handsHuman && window.__handsHuman(Object.assign({frame: window.name || (window.top === window ? 'top' : 'frame')}, o)); } catch (e) {} };
  const desc = el => ({ tag: (el.tagName || '').toLowerCase(), type: (el.type || '').toLowerCase(),
                        name_attr: el.getAttribute ? (el.getAttribute('name') || '') : '',
                        label: norm(el.innerText || el.value || el.getAttribute('aria-label') || '') });
  addEventListener('click', e => { const t = e.target.closest ? e.target.closest('a,button,input,select,[onclick]') : null; if (t) send({kind: 'click', target: desc(t)}); }, true);
  addEventListener('change', e => { const t = e.target; send({kind: t.type === 'checkbox' || t.type === 'radio' ? 'check' : t.tagName === 'SELECT' ? 'select' : 'input',
                                   target: desc(t), value_len: t.type === 'password' ? -1 : (t.value || '').length}); }, true);
  addEventListener('submit', e => send({kind: 'submit', target: desc(e.target)}), true);
})();
"""

# Regexes the screenshot masker hides even if a value was never "learned".
_SCREEN_PATTERNS = [r"\b\d{3}-\d{2}-\d{4}\b", r"\(\d{3}\)\s?\d{3}-\d{4}\b"]


class PlaywrightSurface:
    def __init__(self, policy: Policy, redactor: Redactor, headless: bool = True,
                 guard: Callable[[], None] = lambda: None, viewport: tuple[int, int] = (1100, 700)):
        self.policy = policy
        self.redactor = redactor
        self.headless = headless
        self.guard = guard                  # raises unless automation currently owns the session
        self.viewport = viewport
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._inflight = 0
        self._status: dict[str, int] = {}
        self._dialog: str | None = None
        self.blocked_requests: list[tuple[str, str]] = []
        self._capturing = False
        self._human: list[dict] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "PlaywrightSurface":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.context = self._browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]})
        self.context.route("**/*", self._route_guard)
        self.context.expose_binding("__handsHuman", lambda _src, ev: self._on_human(ev))
        self.context.add_init_script(_INIT_HUMAN)
        self.page = self.context.new_page()
        self.page.on("request", lambda _r: self._bump(1))
        self.page.on("requestfinished", lambda _r: self._bump(-1))
        self.page.on("requestfailed", lambda _r: self._bump(-1))
        self.page.on("response", self._on_response)
        self.page.on("dialog", self._on_dialog)
        self.page.on("framenavigated", self._on_nav)
        return self

    def close(self) -> None:
        for closer in (lambda: self.context and self.context.close(),
                       lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ network-level guard
    def _route_guard(self, route) -> None:
        url = route.request.url
        ok, why = self.policy.url_allowed(url)
        if ok:
            route.continue_()
        else:
            self.blocked_requests.append((url, why))
            route.abort("blockedbyclient")

    def _bump(self, n: int) -> None:
        self._inflight = max(0, self._inflight + n)

    def _fkey(self, fr: Frame) -> str:
        if fr.parent_frame is None:
            return "top"
        return fr.name or f"frame-{self.page.frames.index(fr)}"

    def _on_response(self, resp) -> None:
        try:
            if resp.request.resource_type == "document":
                self._status[self._fkey(resp.frame)] = resp.status
        except Exception:
            pass

    def _on_dialog(self, dialog) -> None:
        # Never auto-accept: a confirm() is a decision. Cancel it and surface it as state.
        self._dialog = f"{dialog.type}: {dialog.message}"
        try:
            dialog.dismiss()
        except Exception:
            pass

    def _on_nav(self, fr: Frame) -> None:
        if self._capturing:
            try:
                path = fr.url.split("://", 1)[-1].split("/", 1)[-1]
                self._human.append({"kind": "navigate", "frame": self._fkey(fr), "path": "/" + path.split("?")[0]})
            except Exception:
                pass

    # ------------------------------------------------------------------ human-capture seam
    def begin_human_capture(self) -> None:
        self._human, self._capturing = [], True

    def end_human_capture(self) -> list[dict]:
        self._capturing = False
        return self._human

    def _on_human(self, ev: dict) -> None:
        if self._capturing:
            self._human.append(ev)

    # ------------------------------------------------------------------ perceive
    def _frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def observe(self) -> Observation:
        elements: list[Element] = []
        frames: list[FrameState] = []
        for fr in self._frames():
            try:
                data = fr.evaluate(_JS_COLLECT, len(elements))
            except Exception:
                continue        # frame mid-navigation; the next observation will see it
            key = self._fkey(fr)
            fields = [(l, v) for l, v in data["fields"]]
            self.redactor.learn_fields(fields)
            frames.append(FrameState(name=key, url=data["url"], status=self._status.get(key),
                                     title=data["title"], text=data["text"], fields=fields))
            for e in data["elements"]:
                elements.append(Element(frame=key, **e))
        dialog, self._dialog = self._dialog, None
        return Observation(frames=frames, elements=elements, dialog=dialog)

    def screenshot(self, masked: bool = True) -> bytes:
        undo: list[Frame] = []
        try:
            if masked:
                args = {"masks": self.redactor.screen_masks(), "patterns": _SCREEN_PATTERNS}
                for fr in self._frames():
                    try:
                        fr.evaluate(_JS_MASK, args)
                        undo.append(fr)
                    except Exception:
                        pass
            return self.page.screenshot(type="png")
        finally:
            for fr in undo:
                try:
                    fr.evaluate("() => window.__handsUndo && window.__handsUndo()")
                except Exception:
                    pass

    def snapshot_text(self) -> str:
        out = []
        for fr in self._frames():
            try:
                out.append(f"<!-- frame {self._fkey(fr)} {fr.url} status={self._status.get(self._fkey(fr))} -->\n{fr.content()}")
            except Exception:
                pass
        return "\n".join(out)

    # ------------------------------------------------------------------ act
    def _frame(self, name: str) -> Frame:
        for fr in self._frames():
            if self._fkey(fr) == name:
                return fr
        raise LookupError(f"frame {name!r} is gone")

    def perform(self, action: ActionType, el: Element | None, value: str | bool | None = None) -> None:
        self.guard()
        t = self.policy.step_timeout_ms
        if action == ActionType.press:
            self.page.keyboard.press(str(value))
        else:
            assert el is not None, f"{action.value} needs a target"
            loc = self._frame(el.frame).locator(f'[data-hands-ref="{el.ref}"]')
            if action == ActionType.click:
                loc.click(timeout=t)
            elif action == ActionType.fill:
                loc.fill("" if value is None else str(value), timeout=t)
            elif action == ActionType.select:
                try:
                    loc.select_option(value=str(value), timeout=t)
                except Exception:
                    loc.select_option(label=str(value), timeout=t)
            elif action == ActionType.check:
                loc.check(timeout=t) if value else loc.uncheck(timeout=t)
            else:
                raise ValueError(f"surface cannot perform {action.value}")
        self.settle()

    def goto(self, url: str) -> None:
        self.guard()
        self.policy.check_step_url(url)
        self.page.goto(url, wait_until="load")
        self.settle()

    def reload_frame(self, name: str) -> None:
        self.guard()
        fr = self._frame(name)
        fr.goto(fr.url, wait_until="load")
        self.settle()

    def settle(self, timeout_ms: int = 10000, quiet_ms: int = 200) -> bool:
        """Condition-based wait (no fixed sleeps): no requests in flight and every frame
        loaded, continuously, for `quiet_ms`."""
        deadline = time.monotonic() + timeout_ms / 1000
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            ready = self._inflight == 0
            if ready:
                for fr in self._frames():
                    try:
                        if fr.evaluate("document.readyState") != "complete":
                            ready = False
                            break
                    except Exception:
                        ready = False
                        break
            if ready:
                quiet_since = quiet_since or time.monotonic()
                if (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
                    return True
            else:
                quiet_since = None
            self.page.wait_for_timeout(40)
        return False

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    @property
    def frame_status(self) -> dict[str, int]:
        return dict(self._status)


__all__ = ["Surface", "PlaywrightSurface", "PolicyViolation"]
