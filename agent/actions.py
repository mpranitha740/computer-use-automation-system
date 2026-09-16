"""
The actuator: the single place that turns a Locator (role+name, with
fallbacks) into a Playwright action. Both the discovery loop (recording what
it did) and the replay engine (re-executing what was recorded) call through
here, so "how we act on a surface" has exactly one implementation.
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from agent.schemas import Locator


class ElementNotFound(Exception):
    def __init__(self, locator: Locator, tried: list[str]):
        self.locator = locator
        self.tried = tried
        super().__init__(f"no strategy matched for {locator.description!r}; tried: {tried}")


@dataclass
class ResolvedLocator:
    playwright_locator: object
    matched_strategy: dict[str, str]


def resolve(page: Page, locator: Locator, timeout_ms: int = 5000):
    """Try each strategy in order; return the first that matches >=1 visible
    element. Records which strategy actually worked, which is the signal
    that a recorded flow has started drifting even though it still passes."""
    tried = []
    for strat in locator.strategies:
        kind = strat["strategy"]
        value = strat["value"]
        try:
            if kind == "role":
                loc = page.get_by_role(strat.get("role", "button"), name=value, exact=False)
            elif kind == "text":
                loc = page.get_by_text(value, exact=False)
            elif kind == "label":
                loc = page.get_by_label(value, exact=False)
            elif kind == "css":
                loc = page.locator(value)
            elif kind == "xpath":
                loc = page.locator(f"xpath={value}")
            elif kind == "nth":
                tag, nth_str = value.split("|")
                loc = page.locator(tag).nth(int(nth_str))
            else:
                tried.append(f"{kind}:unknown-strategy")
                continue
            loc.first.wait_for(state="attached", timeout=timeout_ms)
            if loc.count() >= 1:
                return ResolvedLocator(playwright_locator=loc.first, matched_strategy=strat)
            tried.append(f"{kind}:{value} (0 matches)")
        except PWTimeoutError:
            tried.append(f"{kind}:{value} (timeout)")
        except Exception as exc:  # noqa: BLE001 - collapse into "tried" for diagnostics
            tried.append(f"{kind}:{value} ({exc.__class__.__name__})")
    raise ElementNotFound(locator, tried)


def do_click(page: Page, locator: Locator, timeout_ms: int = 5000) -> ResolvedLocator:
    resolved = resolve(page, locator, timeout_ms)
    resolved.playwright_locator.click(timeout=timeout_ms)
    return resolved


def do_fill(page: Page, locator: Locator, value: str, timeout_ms: int = 5000) -> ResolvedLocator:
    resolved = resolve(page, locator, timeout_ms)
    resolved.playwright_locator.fill(value, timeout=timeout_ms)
    return resolved


def do_select_option(page: Page, locator: Locator, value: str, timeout_ms: int = 5000) -> ResolvedLocator:
    resolved = resolve(page, locator, timeout_ms)
    resolved.playwright_locator.select_option(label=value, timeout=timeout_ms)
    return resolved


def do_extract(page: Page, locator: Locator, timeout_ms: int = 5000) -> tuple[str, ResolvedLocator]:
    resolved = resolve(page, locator, timeout_ms)
    text = resolved.playwright_locator.inner_text(timeout=timeout_ms)
    return text.strip(), resolved


def do_navigate(page: Page, url: str, timeout_ms: int = 8000) -> None:
    page.goto(url, timeout=timeout_ms, wait_until="load")
