"""
Perception layer: turns a live page into a compact, model-readable snapshot
without assuming a clean DOM or test IDs.

This is the seam the rest of the system is built around: everything above
this line (the LLM loop, the artifact, the replay engine) talks in terms of
`ElementRef`s (role + accessible name + a short-lived index), never raw CSS
selectors scraped off a specific markup shape. That's what lets the same
agent loop and the same replay engine work against a legacy, non-semantic
DOM: the extraction step here is the only thing that would need to change
to support a different surface (e.g. an OS accessibility tree for a desktop
app, or OCR+coordinates for a screenshot-only surface) -- see REPORT.md
section 4.

We deliberately use *role + accessible name*, the same identity a screen
reader would use, as the primary locator. It survives markup refactors
(div-turned-table, class renames) that break naive CSS selectors, and it's
available even in legacy server-rendered HTML because the browser computes
accessible name/role from tag semantics (button, a, input+label) regardless
of whether the developer added any automation hooks.
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from agent.schemas import Locator

INTERACTIVE_SELECTOR = "a, button, input, select, textarea, [onclick]"


@dataclass
class ElementRef:
    ref: str            # short id used by the LLM to refer to this element, e.g. "e3"
    role: str            # accessibility role, e.g. "button", "textbox", "link"
    name: str            # accessible name (visible text / label / placeholder)
    tag: str
    input_type: str | None = None
    current_value: str | None = None
    attr_name: str | None = None   # raw HTML name= attribute, if present (legacy fallback)
    nth_of_tag: int = 0            # this element's index among same-tag elements (last-resort fallback)


ROLE_BY_TAG = {
    "a": "link",
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
}


def _role_for(tag: str, input_type: str | None) -> str:
    if tag == "input":
        if input_type in ("submit", "button"):
            return "button"
        return "textbox"
    return ROLE_BY_TAG.get(tag, tag)


def snapshot(page: Page) -> tuple[list[ElementRef], str, str, str]:
    """Returns (elements, url, title, visible_text_summary)."""
    raw = page.eval_on_selector_all(
        INTERACTIVE_SELECTOR,
        """(els) => {
            const counts = {};
            return els.map((el) => {
                const tag = el.tagName.toLowerCase();
                counts[tag] = (counts[tag] || 0);
                const nth = counts[tag];
                counts[tag] += 1;
                return {
                    tag: tag,
                    type: el.getAttribute('type'),
                    text: (el.innerText || el.value || el.getAttribute('placeholder') || '').trim().slice(0, 120),
                    value: tag === 'select' ? (el.options[el.selectedIndex]?.text || '') : (el.value || ''),
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                    attrName: el.getAttribute('name'),
                    nth: nth,
                };
            });
        }""",
    )
    elements: list[ElementRef] = []
    for i, item in enumerate(raw):
        if not item["visible"]:
            continue
        role = _role_for(item["tag"], item.get("type"))
        elements.append(
            ElementRef(
                ref=f"e{i}",
                role=role,
                name=item["text"] or "(unlabeled)",
                tag=item["tag"],
                input_type=item.get("type"),
                current_value=item.get("value") or None,
                attr_name=item.get("attrName") or None,
                nth_of_tag=item.get("nth", 0),
            )
        )
    body_text = page.eval_on_selector("body", "(el) => el.innerText") or ""
    text_summary = "\n".join(line.strip() for line in body_text.splitlines() if line.strip())[:2000]
    return elements, page.url, page.title(), text_summary


def build_locator(el: ElementRef, description: str | None = None) -> Locator:
    """Ranked fallback chain, strongest first:
    1. role + accessible name (survives markup/class changes)
    2. raw name= attribute, if the legacy form happens to have one
    3. nth-of-tag CSS (last resort; brittle to reordering, but always available)
    """
    strategies: list[dict[str, str]] = []
    if el.name and el.name != "(unlabeled)":
        strategies.append({"strategy": "role", "role": el.role, "value": el.name})
    if el.attr_name:
        strategies.append({"strategy": "css", "value": f'{el.tag}[name="{el.attr_name}"]'})
    strategies.append({"strategy": "nth", "value": f"{el.tag}|{el.nth_of_tag}"})
    return Locator(strategies=strategies, description=description or f"{el.role} '{el.name}'")
