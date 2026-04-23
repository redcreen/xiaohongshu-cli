"""Browser-context note reading using Camoufox.

This path is reserved for cases where direct API/HTML fetches do not
carry enough browser state to read a public note successfully.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from .constants import HOME_URL
from .exceptions import XhsApiError
from .html_parser import extract_note_from_html, extract_note_from_state


def _ensure_camoufox_ready() -> None:
    try:
        import camoufox  # noqa: F401
    except ImportError as exc:
        raise XhsApiError("Browser-context note reading requires the `camoufox` package.") from exc

    try:
        result = subprocess.run(
            [sys.executable, "-m", "camoufox", "path"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XhsApiError("Unable to validate the Camoufox browser installation.") from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise XhsApiError("Camoufox browser runtime is missing. Run `python -m camoufox fetch` first.")


def _browser_cookies(cookies: dict[str, str]) -> list[dict[str, object]]:
    return [
        {
            "name": str(name),
            "value": str(value),
            "domain": ".xiaohongshu.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        }
        for name, value in (cookies or {}).items()
        if str(name).strip() and str(value).strip()
    ]


def _wrap_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": str(note.get("note_id", note.get("id", ""))).strip(),
                "note_card": note,
            }
        ]
    }


def _has_wrapped_note(payload: dict[str, Any]) -> bool:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return False
    first = items[0] if isinstance(items[0], dict) else {}
    note = first.get("note_card") if isinstance(first, dict) else {}
    if not isinstance(note, dict):
        return False
    return any(
        note.get(key)
        for key in ("title", "desc", "user", "image_list", "video", "note_id", "interact_info")
    )


def get_note_detail_via_browser(
    *,
    note_id: str,
    note_url: str = "",
    cookies: dict[str, str] | None = None,
    timeout_ms: int = 20_000,
    headless: bool = True,
) -> dict[str, Any]:
    _ensure_camoufox_ready()

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise XhsApiError("Camoufox sync API is unavailable in the current environment.") from exc

    target_url = str(note_url or "").strip() or f"{HOME_URL}/explore/{note_id}"
    with Camoufox(headless=headless) as browser:
        page = browser.new_page()
        captured_payloads: list[dict[str, Any]] = []

        def _handle_response(response) -> None:
            if "/api/sns/web/v1/feed" not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if isinstance(payload, dict):
                captured_payloads.append(payload)

        page.on("response", _handle_response)
        browser_cookies = _browser_cookies(cookies or {})
        if browser_cookies:
            page.context.add_cookies(browser_cookies)

        # Warm the origin first so challenge scripts and cookies can settle.
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(800)
        page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        page.wait_for_timeout(1_200)

        final_url = page.url
        html = page.content()
        for payload in reversed(captured_payloads):
            if _has_wrapped_note(payload):
                return payload
        try:
            wrapped = _wrap_note(extract_note_from_html(html, note_id, final_url=final_url))
            if _has_wrapped_note(wrapped):
                return wrapped
        except Exception:
            state = page.evaluate("() => window.__INITIAL_STATE__ || null")
            if isinstance(state, dict):
                wrapped = _wrap_note(extract_note_from_state(state, note_id))
                if _has_wrapped_note(wrapped):
                    return wrapped
            raise XhsApiError("Browser context did not expose note detail")
        raise XhsApiError("Browser context did not expose note detail")
