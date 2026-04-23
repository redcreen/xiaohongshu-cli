"""Best-effort xsec refresh by asking an already-running Chrome session to open the note."""

from __future__ import annotations

import os
import subprocess
from typing import Iterable

from .constants import HOME_URL
from .exceptions import XhsApiError
from .formatter import parse_note_reference

_LIVE_CHROME_REFRESH_ENV = "XHS_ENABLE_LIVE_CHROME_XSEC_REFRESH"
_LIVE_CHROME_APP_ENV = "XHS_LIVE_CHROME_APP_NAME"
_DEFAULT_LIVE_CHROME_APP = "Google Chrome"


def live_chrome_refresh_enabled() -> bool:
    value = str(os.environ.get(_LIVE_CHROME_REFRESH_ENV, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def refresh_note_url_via_live_chrome(
    *,
    note_id: str,
    note_url: str = "",
    timeout_seconds: int = 30,
    app_name: str | None = None,
) -> str:
    if not live_chrome_refresh_enabled():
        return ""

    browser_name = str(app_name or os.environ.get(_LIVE_CHROME_APP_ENV) or _DEFAULT_LIVE_CHROME_APP).strip()
    candidates = list(_candidate_urls(note_id=note_id, note_url=note_url))
    for candidate in candidates:
        final_url = _open_note_in_live_chrome(
            app_name=browser_name,
            target_url=candidate,
            note_id=note_id,
            timeout_seconds=timeout_seconds,
        )
        if _is_usable_note_url(final_url, note_id):
            return final_url
    return ""


def _candidate_urls(*, note_id: str, note_url: str) -> Iterable[str]:
    seen: set[str] = set()
    for candidate in (
        str(note_url or "").strip(),
        f"{HOME_URL}/explore/{note_id}",
    ):
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _is_usable_note_url(url: str, note_id: str) -> bool:
    parsed_note_id, token, _source = parse_note_reference(str(url or "").strip())
    if parsed_note_id != note_id:
        return False
    return bool(token)


def _applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _open_note_in_live_chrome(
    *,
    app_name: str,
    target_url: str,
    note_id: str,
    timeout_seconds: int,
) -> str:
    script = f'''
tell application "{_applescript_string(app_name)}"
  if not running then error "Chrome is not running"
  activate
  if (count of windows) = 0 then make new window
  tell front window
    set newTab to make new tab with properties {{URL:"{_applescript_string(target_url)}"}}
    repeat {max(1, int(timeout_seconds))}
      delay 1
      try
        set currentURL to URL of newTab
      on error
        set currentURL to ""
      end try
      if currentURL contains "{_applescript_string(note_id)}" and currentURL contains "xsec_token=" then
        return currentURL
      end if
      if currentURL contains "/website-login/error" then
        return currentURL
      end if
    end repeat
    try
      return URL of newTab
    on error
      return ""
    end try
  end tell
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=max(10, int(timeout_seconds) + 5),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XhsApiError(f"Live Chrome refresh failed: {exc}") from exc

    output = str(result.stdout or "").strip()
    error_text = str(result.stderr or "").strip()
    if result.returncode != 0:
        lowered = error_text.lower()
        if "-1743" in error_text or "not authorized" in lowered or "未获得授权" in error_text:
            raise XhsApiError("Live Chrome refresh requires macOS Automation permission for Google Chrome.")
        raise XhsApiError(f"Live Chrome refresh failed: {error_text or output or 'unknown AppleScript error'}")
    return output
