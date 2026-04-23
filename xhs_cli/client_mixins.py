"""Domain-specific endpoint mixins for XhsClient."""

from __future__ import annotations

import json
import logging
import mimetypes
import random
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .browser_context import get_note_detail_via_browser
from .constants import CREATOR_HOST, HOME_URL, UPLOAD_HOST, USER_AGENT
from .cookies import (
    cache_note_context,
    cookies_to_string,
    get_cached_note_context,
    get_config_dir,
    invalidate_note_context,
)
from .exceptions import NeedVerifyError, SessionExpiredError, UnsupportedOperationError, XhsApiError
from .formatter import parse_note_reference
from .html_parser import extract_note_from_html
from .live_chrome_refresh import refresh_note_url_via_live_chrome

logger = logging.getLogger(__name__)

_SEARCH_DEFAULT_FILTERS = [
    {"tags": ["general"], "type": "sort_type"},
    {"tags": ["不限"], "type": "filter_note_type"},
    {"tags": ["不限"], "type": "filter_note_time"},
    {"tags": ["不限"], "type": "filter_note_range"},
    {"tags": ["不限"], "type": "filter_pos_distance"},
]
_SEARCH_SESSION_TTL_SECONDS = 600
_SEARCH_SESSION_MAX_SIZE = 128
_SEARCH_SESSION_LOCK = threading.RLock()
_SEARCH_SESSION_CACHE: OrderedDict[tuple[str, str, int], dict[str, Any]] = OrderedDict()
_SEARCH_SESSION_CACHE_PATH: Path | None = None
_SEARCH_SESSION_CACHE_LOADED = False


def _generate_search_id() -> str:
    """Generate a unique search ID (base36 of timestamp << 64 + random)."""
    e = int(time.time() * 1000) << 64
    t = random.randint(0, 2147483646)
    num = e + t

    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if num == 0:
        return "0"
    result = ""
    while num > 0:
        result = alphabet[num % 36] + result
        num //= 36
    return result


def _search_session_key(keyword: str, sort: str, note_type: int) -> tuple[str, str, int]:
    return (keyword.strip(), sort, note_type)


def _search_session_path() -> Path:
    return get_config_dir() / "search_sessions.json"


def _serialize_search_session_key(key: tuple[str, str, int]) -> str:
    return json.dumps([key[0], key[1], key[2]], ensure_ascii=False)


def _deserialize_search_session_key(value: str) -> tuple[str, str, int] | None:
    try:
        keyword, sort, note_type = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(keyword, str) or not isinstance(sort, str):
        return None
    try:
        normalized_note_type = int(note_type)
    except (TypeError, ValueError):
        return None
    return (keyword, sort, normalized_note_type)


def _load_search_session_cache_from_disk(path: Path) -> OrderedDict[tuple[str, str, int], dict[str, Any]]:
    if not path.exists():
        return OrderedDict()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return OrderedDict()
    if not isinstance(data, dict):
        return OrderedDict()

    normalized: list[tuple[tuple[str, str, int], dict[str, Any]]] = []
    for raw_key, value in data.items():
        key = _deserialize_search_session_key(raw_key)
        if not key or not isinstance(value, dict):
            continue
        if not value.get("search_id"):
            continue
        normalized.append((key, {
            "search_id": str(value["search_id"]),
            "created_at": float(value.get("created_at", 0) or 0),
            "last_used_at": float(value.get("last_used_at", 0) or 0),
        }))
    normalized.sort(key=lambda item: float(item[1].get("last_used_at", 0)))
    return OrderedDict(normalized)


def _save_search_session_cache(path: Path) -> None:
    payload = OrderedDict(
        (
            _serialize_search_session_key(key),
            dict(value),
        )
        for key, value in _SEARCH_SESSION_CACHE.items()
    )
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def _ensure_search_session_cache_loaded() -> None:
    global _SEARCH_SESSION_CACHE_LOADED, _SEARCH_SESSION_CACHE_PATH, _SEARCH_SESSION_CACHE
    path = _search_session_path()
    if _SEARCH_SESSION_CACHE_LOADED and _SEARCH_SESSION_CACHE_PATH == path:
        return
    _SEARCH_SESSION_CACHE = _load_search_session_cache_from_disk(path)
    _SEARCH_SESSION_CACHE_PATH = path
    _SEARCH_SESSION_CACHE_LOADED = True


def _prune_search_sessions(now: float) -> None:
    expired_keys = [
        key
        for key, value in _SEARCH_SESSION_CACHE.items()
        if now - float(value.get("last_used_at", 0)) > _SEARCH_SESSION_TTL_SECONDS
    ]
    for key in expired_keys:
        _SEARCH_SESSION_CACHE.pop(key, None)

    while len(_SEARCH_SESSION_CACHE) > _SEARCH_SESSION_MAX_SIZE:
        _SEARCH_SESSION_CACHE.popitem(last=False)


def _acquire_search_session(keyword: str, sort: str, note_type: int) -> tuple[str, bool]:
    now = time.time()
    key = _search_session_key(keyword, sort, note_type)

    with _SEARCH_SESSION_LOCK:
        _ensure_search_session_cache_loaded()
        _prune_search_sessions(now)
        existing = _SEARCH_SESSION_CACHE.get(key)
        if existing:
            existing["last_used_at"] = now
            _SEARCH_SESSION_CACHE.move_to_end(key)
            _save_search_session_cache(_SEARCH_SESSION_CACHE_PATH or _search_session_path())
            return str(existing["search_id"]), False

        search_id = _generate_search_id()
        _SEARCH_SESSION_CACHE[key] = {
            "search_id": search_id,
            "created_at": now,
            "last_used_at": now,
        }
        _save_search_session_cache(_SEARCH_SESSION_CACHE_PATH or _search_session_path())
        return search_id, True


def get_search_session_stats() -> dict[str, Any]:
    """Return lightweight debug stats for the in-memory search session cache."""
    now = time.time()
    with _SEARCH_SESSION_LOCK:
        _ensure_search_session_cache_loaded()
        _prune_search_sessions(now)
        if not _SEARCH_SESSION_CACHE:
            return {
                "active_count": 0,
                "last_keyword": "",
                "last_sort": "",
                "last_note_type": None,
            }

        last_key = next(reversed(_SEARCH_SESSION_CACHE))
        return {
            "active_count": len(_SEARCH_SESSION_CACHE),
            "last_keyword": last_key[0],
            "last_sort": last_key[1],
            "last_note_type": last_key[2],
        }


class ReadingEndpointsMixin:
    """Read-only note, profile, and discovery endpoints."""

    @staticmethod
    def _has_note_detail_content(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        items = payload.get("items")
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            note = first.get("note_card") if isinstance(first, dict) else {}
            if isinstance(note, dict):
                return any(
                    note.get(key)
                    for key in ("title", "desc", "user", "image_list", "video", "note_id", "interact_info")
                )
        return bool(payload.get("noteId") or payload.get("note_id") or payload.get("title"))

    def _search_request_id(self) -> str:
        return f"{random.randint(1_000_000_000, 2_147_483_647)}-{int(time.time() * 1000)}"

    def _fetch_note_html(
        self,
        note_id: str,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> tuple[str, str]:
        if xsec_token:
            url = f"{HOME_URL}/explore/{note_id}?xsec_token={xsec_token}&xsec_source={xsec_source}"
        else:
            url = f"{HOME_URL}/explore/{note_id}"

        resp = self._request_with_retry(
            "GET",
            url,
            headers={
                "user-agent": USER_AGENT,
                "referer": f"{HOME_URL}/",
                "cookie": cookies_to_string(self.cookies),
            },
        )
        return resp.text, str(resp.url)

    def _fetch_note_reference_html(self, note_id: str, note_url: str = "") -> tuple[str, str]:
        reference = str(note_url or "").strip()
        if not reference or "xiaohongshu.com" not in reference:
            return self._fetch_note_html(note_id)

        resp = self._request_with_retry(
            "GET",
            reference,
            headers={
                "user-agent": USER_AGENT,
                "referer": f"{HOME_URL}/",
                "cookie": cookies_to_string(self.cookies),
            },
        )
        return resp.text, str(resp.url)

    def resolve_xsec_context(
        self,
        note_id: str,
        preferred_token: str = "",
        preferred_source: str = "",
        note_url: str = "",
    ) -> tuple[str, str]:
        """Resolve xsec_token/xsec_source from input, cache, or note page metadata."""
        if preferred_token:
            cache_note_context(note_id, preferred_token, preferred_source)
            return preferred_token, preferred_source

        cached = get_cached_note_context(note_id)
        if cached.get("token"):
            return cached["token"], cached.get("source", "")

        html, final_url = self._fetch_note_reference_html(note_id, note_url=note_url)
        patterns = [
            r'"xsec_token"\s*:\s*"([^"]+)"',
            r"xsec_token=([^&\"']+)",
            r"'xsec_token':'([^']+)'",
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                token = match.group(1)
                source_match = re.search(r"xsec_source=([^&\"']+)", html)
                if source_match:
                    source = source_match.group(1)
                else:
                    final_source_match = re.search(r"xsec_source=([^&\"']+)", final_url)
                    source = final_source_match.group(1) if final_source_match else preferred_source
                cache_note_context(note_id, token, source)
                return token, source
        return "", preferred_source

    def resolve_xsec_token(self, note_id: str, preferred_token: str = "") -> str:
        """Resolve xsec_token from explicit input, cache, or note page metadata."""
        token, _source = self.resolve_xsec_context(note_id, preferred_token)
        return token

    def get_self_info(self) -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v2/user/me")

    def get_user_info(self, user_id: str) -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v1/user/otherinfo", {
            "target_user_id": user_id,
        })

    def get_user_notes(self, user_id: str, cursor: str = "") -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v1/user_posted", {
            "num": 30,
            "cursor": cursor,
            "user_id": user_id,
            "image_scenes": "FD_WM_WEBP",
        })

    def search_notes(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        sort: str = "general",
        note_type: int = 0,
    ) -> Any:
        search_id, is_new_session = _acquire_search_session(keyword, sort, note_type)
        if is_new_session:
            request_id = self._search_request_id()
            try:
                self._main_api_post("/api/sns/web/v1/search/onebox", {
                    "keyword": keyword,
                    "search_id": search_id,
                    "biz_type": "web_search_user",
                    "request_id": request_id,
                })
                self._main_api_get("/api/sns/web/v1/search/filter", {
                    "keyword": keyword,
                    "search_id": search_id,
                })
            except XhsApiError as exc:
                logger.debug("Search prewarm failed, continuing with search/notes: %s", exc)

        result = self._main_api_post("/api/sns/web/v1/search/notes", {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": search_id,
            "sort": sort,
            "note_type": note_type,
            "ext_flags": [],
            "filters": _SEARCH_DEFAULT_FILTERS,
            "geo": "",
            "image_formats": ["jpg", "webp", "avif"],
        })
        if is_new_session:
            try:
                self._main_api_get("/api/sns/web/v1/search/recommend", {"keyword": keyword})
            except XhsApiError as exc:
                logger.debug("Search recommend prefetch failed: %s", exc)
        return result

    def get_note_by_id(
        self,
        note_id: str,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> Any:
        if xsec_token:
            cache_note_context(note_id, xsec_token, xsec_source)
        return self._main_api_post("/api/sns/web/v1/feed", {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": xsec_source,
            "xsec_token": xsec_token,
        })

    def get_note_from_html(
        self,
        note_id: str,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> dict[str, Any]:
        """Fetch note by parsing server-rendered HTML (no xsec_token required)."""
        html, final_url = self._fetch_note_html(note_id, xsec_token=xsec_token, xsec_source=xsec_source)
        note = extract_note_from_html(html, note_id, final_url=final_url)
        return {"items": [{"id": str(note.get("note_id", note.get("id", note_id))), "note_card": note}]}

    def get_note_detail(
        self,
        note_id: str,
        xsec_token: str = "",
        xsec_source: str = "",
        note_url: str = "",
    ) -> dict[str, Any]:
        """Read a note via the best available channel.

        Strategy:
          - Has xsec_token → try feed API first, fall back to HTML on error
          - No xsec_token  → go straight to HTML (feed API would reject)
        """
        cached = get_cached_note_context(note_id)
        token = xsec_token or cached.get("token", "")
        source = xsec_source or cached.get("source", "") or "pc_feed"
        used_cached_context = not xsec_token and bool(cached.get("token"))
        if token:
            try:
                response = self.get_note_by_id(note_id, xsec_token=token, xsec_source=source)
                if self._has_note_detail_content(response):
                    return response
                logger.info("Feed API returned no note items, refreshing xsec context from HTML")
            except (NeedVerifyError, XhsApiError) as exc:
                logger.info("Feed API failed (%s), falling back to HTML", exc)
            invalidate_note_context(note_id)
            refreshed_token, refreshed_source = self.resolve_xsec_context(note_id, "", source, note_url=note_url)
            if refreshed_token:
                try:
                    response = self.get_note_by_id(note_id, xsec_token=refreshed_token, xsec_source=refreshed_source or source)
                    if self._has_note_detail_content(response):
                        return response
                    logger.info("Refreshed xsec context still returned no items; falling back to HTML")
                except (NeedVerifyError, XhsApiError) as exc:
                    logger.info("Refreshed feed API failed (%s), falling back to HTML", exc)
            elif used_cached_context:
                token = ""
        response = self.get_note_from_html(note_id, xsec_token="", xsec_source=source)
        if self._has_note_detail_content(response):
            return response
        live_refreshed_url = ""
        if note_url and getattr(self, "enable_live_chrome_xsec_refresh", False):
            try:
                live_refreshed_url = refresh_note_url_via_live_chrome(
                    note_id=note_id,
                    note_url=note_url,
                )
            except XhsApiError as exc:
                logger.info("Live Chrome xsec refresh failed (%s)", exc)
            if live_refreshed_url:
                logger.info("Live Chrome refresh resolved a fresh note URL; retrying feed API")
                _ref_note_id, live_token, live_source = parse_note_reference(live_refreshed_url)
                if live_token:
                    cache_note_context(note_id, live_token, live_source)
                    try:
                        response = self.get_note_by_id(
                            note_id,
                            xsec_token=live_token,
                            xsec_source=live_source or source,
                        )
                        if self._has_note_detail_content(response):
                            return response
                    except (NeedVerifyError, XhsApiError) as exc:
                        logger.info("Live Chrome refreshed feed API failed (%s)", exc)
                note_url = live_refreshed_url
        if getattr(self, "enable_browser_context_fallback", False):
            logger.info("HTML fallback still has no note data, trying browser-context fallback")
            return get_note_detail_via_browser(
                note_id=note_id,
                note_url=live_refreshed_url or note_url,
                cookies=getattr(self, "cookies", {}) or {},
            )
        raise XhsApiError("No note data found after refreshing xsec context")

    def get_home_feed(self, category: str = "homefeed_recommend") -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/homefeed", {
            "cursor_score": "",
            "num": 40,
            "refresh_type": 1,
            "note_index": 0,
            "unread_begin_note_id": "",
            "unread_end_note_id": "",
            "unread_note_count": 0,
            "category": category,
            "search_key": "",
            "need_num": 40,
            "image_scenes": ["FD_PRV_WEBP", "FD_WM_WEBP"],
        })

    def get_hot_feed(self, category: str = "homefeed.fashion_v3") -> dict[str, Any]:
        return self.get_home_feed(category=category)

    def get_comments(
        self,
        note_id: str,
        cursor: str = "",
        xsec_token: str = "",
        top_comment_id: str = "",
        xsec_source: str = "",
        note_url: str = "",
    ) -> Any:
        cached = get_cached_note_context(note_id)
        used_cached_context = not xsec_token and bool(cached.get("token"))
        token, source = self.resolve_xsec_context(note_id, xsec_token, xsec_source, note_url=note_url)
        if not token:
            raise XhsApiError(
                "Could not resolve xsec_token for comments. Pass a full note URL or --xsec-token explicitly."
            )
        if source:
            cache_note_context(note_id, token, source)

        def _request_comments(active_token: str) -> Any:
            payload = {
                "note_id": note_id,
                "cursor": cursor,
                "top_comment_id": top_comment_id,
                "image_formats": "jpg,webp,avif",
                "xsec_token": active_token,
            }
            if source:
                payload["xsec_source"] = source
            return self._main_api_get("/api/sns/web/v2/comment/page", payload)

        try:
            return _request_comments(token)
        except NeedVerifyError:
            raise
        except SessionExpiredError:
            # Warm the note detail in-process so response cookies and token cache can refresh
            self.get_note_detail(note_id, xsec_token=token, xsec_source=source, note_url=note_url)
            refreshed_token, refreshed_source = self.resolve_xsec_context(note_id, "", source, note_url=note_url)
            if not refreshed_token:
                raise
            if refreshed_source:
                cache_note_context(note_id, refreshed_token, refreshed_source)
            return _request_comments(refreshed_token)
        except XhsApiError:
            if not used_cached_context:
                raise
            invalidate_note_context(note_id)
            refreshed_token, refreshed_source = self.resolve_xsec_context(note_id, "", xsec_source, note_url=note_url)
            if not refreshed_token:
                raise
            if refreshed_source:
                cache_note_context(note_id, refreshed_token, refreshed_source)
            return _request_comments(refreshed_token)

    def get_all_comments(
        self,
        note_id: str,
        xsec_token: str = "",
        xsec_source: str = "",
        note_url: str = "",
        max_pages: int = 20,
    ) -> dict[str, Any]:
        all_comments: list[dict[str, Any]] = []
        cursor = ""
        pages = 0

        while pages < max_pages:
            data = self.get_comments(
                note_id,
                cursor=cursor,
                xsec_token=xsec_token,
                xsec_source=xsec_source,
                note_url=note_url,
            )
            if not isinstance(data, dict):
                break

            comments = data.get("comments", [])
            all_comments.extend(comments)
            pages += 1

            has_more = data.get("has_more", False)
            next_cursor = data.get("cursor", "")
            if not has_more or not next_cursor:
                break
            cursor = next_cursor

        return {
            "comments": all_comments,
            "has_more": False,
            "cursor": "",
            "total_fetched": len(all_comments),
            "pages_fetched": pages,
        }

    def get_sub_comments(
        self,
        note_id: str,
        root_comment_id: str,
        num: int = 30,
        cursor: str = "",
    ) -> Any:
        return self._main_api_get("/api/sns/web/v2/comment/sub/page", {
            "note_id": note_id,
            "root_comment_id": root_comment_id,
            "num": num,
            "cursor": cursor,
        })


class InteractionEndpointsMixin:
    """Mutating note interaction endpoints."""

    def post_comment(self, note_id: str, content: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/comment/post", {
            "note_id": note_id,
            "content": content,
            "at_users": [],
        })

    def reply_comment(self, note_id: str, target_comment_id: str, content: str) -> Any:
        return self._main_api_post("/api/sns/web/v1/comment/post", {
            "note_id": note_id,
            "content": content,
            "target_comment_id": target_comment_id,
            "at_users": [],
        })

    def like_note(self, note_id: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/note/like", {"note_oid": note_id})

    def unlike_note(self, note_id: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/note/dislike", {"note_oid": note_id})

    def favorite_note(self, note_id: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/note/collect", {"note_id": note_id})

    def unfavorite_note(self, note_id: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/note/uncollect", {"note_ids": note_id})

    def delete_comment(self, note_id: str, comment_id: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/comment/delete", {
            "note_id": note_id,
            "comment_id": comment_id,
        })


class CreatorEndpointsMixin:
    """Creator platform search, upload, and publishing endpoints."""

    def search_topics(self, keyword: str) -> dict[str, Any]:
        return self._creator_post("/web_api/sns/v1/search/topic", {
            "keyword": keyword,
            "suggest_topic_request": {"title": "", "desc": ""},
            "page": {"page_size": 20, "page": 1},
        })

    def search_users(self, keyword: str) -> dict[str, Any]:
        return self._creator_post("/web_api/sns/v1/search/user_info", {
            "keyword": keyword,
            "search_id": str(int(time.time() * 1000)),
            "page": {"page_size": 20, "page": 1},
        })

    def get_upload_permit(self, file_type: str = "image", count: int = 1) -> dict[str, str]:
        data = self._creator_get("/api/media/v1/upload/web/permit", {
            "biz_name": "spectrum",
            "scene": file_type,
            "file_count": count,
            "version": 1,
            "source": "web",
        })
        permit = data["uploadTempPermits"][0]
        return {"fileId": permit["fileIds"][0], "token": permit["token"]}

    def upload_file(
        self,
        file_id: str,
        token: str,
        file_path: str,
        content_type: str | None = None,
    ) -> None:
        with open(file_path, "rb") as f:
            file_data = f.read()

        url = f"{UPLOAD_HOST}/{file_id}"
        content_type = content_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        resp = self._request_with_retry(
            "PUT",
            url,
            headers={
                "X-Cos-Security-Token": token,
                "Content-Type": content_type,
            },
            content=file_data,
        )
        if resp.status_code >= 400:
            raise XhsApiError(f"Upload failed: {resp.status_code} {resp.reason_phrase}")

    def create_image_note(
        self,
        title: str,
        desc: str,
        image_file_ids: list[str],
        topics: list[dict[str, str]] | None = None,
        is_private: bool = False,
    ) -> Any:
        images = [{"file_id": fid, "metadata": {"source": -1}} for fid in image_file_ids]
        business_binds = {
            "version": 1,
            "noteId": 0,
            "noteOrderBind": {},
            "notePostTiming": {"postTime": None},
            "noteCollectionBind": {"id": ""},
        }
        data = {
            "common": {
                "type": "normal",
                "title": title,
                "note_id": "",
                "desc": desc,
                "source": '{"type":"web","ids":"","extraInfo":"{\\"subType\\":\\"official\\"}"}',
                "business_binds": json.dumps(business_binds),
                "ats": [],
                "hash_tag": topics or [],
                "post_loc": {},
                "privacy_info": {"op_type": 1, "type": 1 if is_private else 0},
            },
            "image_info": {"images": images},
            "video_info": None,
        }
        return self._main_api_post("/web_api/sns/v2/note", data, {
            "origin": CREATOR_HOST,
            "referer": f"{CREATOR_HOST}/",
        })

    def delete_note(self, note_id: str) -> dict[str, Any]:
        try:
            return self._creator_post("/api/galaxy/creator/note/delete", {
                "note_id": note_id,
            })
        except XhsApiError as exc:
            response = exc.response if isinstance(exc.response, dict) else {}
            if response.get("status") == 404 or "404" in str(exc):
                raise UnsupportedOperationError(
                    "Delete note is currently unavailable from the public web API. "
                    "The command remains experimental until the new endpoint is re-captured."
                ) from None
            raise

    def get_creator_note_list(self, tab: int = 0, page: int = 0) -> dict[str, Any]:
        return self._creator_get("/api/galaxy/v2/creator/note/user/posted", {
            "tab": tab,
            "page": page,
        })


class SocialEndpointsMixin:
    """Social graph and saved-content endpoints."""

    def follow_user(self, user_id: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/user/follow", {"target_user_id": user_id})

    def unfollow_user(self, user_id: str) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/user/unfollow", {"target_user_id": user_id})

    def get_user_favorites(self, user_id: str, cursor: str = "") -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v2/note/collect/page", {
            "user_id": user_id,
            "cursor": cursor,
            "num": 30,
        })

    def get_user_likes(self, user_id: str, cursor: str = "") -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v1/note/like/page", {
            "user_id": user_id,
            "cursor": cursor,
            "num": 30,
        })


class NotificationEndpointsMixin:
    """Notification and unread-count endpoints."""

    def get_unread_count(self) -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/unread_count", {})

    def get_notification_mentions(self, cursor: str = "", num: int = 20) -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v1/you/mentions", {
            "num": num,
            "cursor": cursor,
        })

    def get_notification_likes(self, cursor: str = "", num: int = 20) -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v1/you/likes", {
            "num": num,
            "cursor": cursor,
        })

    def get_notification_connections(self, cursor: str = "", num: int = 20) -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v1/you/connections", {
            "num": num,
            "cursor": cursor,
        })


class AuthEndpointsMixin:
    """Authentication-specific endpoints."""

    def login_activate(self) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/login/activate", {})

    def create_qr_login(self) -> dict[str, Any]:
        return self._main_api_post("/api/sns/web/v1/login/qrcode/create", {"qr_type": 1})

    def check_qr_status(self, qr_id: str, code: str) -> dict[str, Any]:
        return self._main_api_post("/api/qrcode/userinfo", {
            "qrId": qr_id,
            "code": code,
        }, {
            "service-tag": "webcn",
        })

    def complete_qr_login(self, qr_id: str, code: str) -> dict[str, Any]:
        return self._main_api_get("/api/sns/web/v1/login/qrcode/status", {
            "qr_id": qr_id,
            "code": code,
        })
