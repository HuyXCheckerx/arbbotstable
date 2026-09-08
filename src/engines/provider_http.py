"""Classify provider HTTP failures without exposing challenge pages or secrets."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def safe_endpoint(url: str) -> str:
    """Keep the endpoint while discarding credentials, query values and fragments."""
    try:
        parts = urlsplit(str(url))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "<invalid endpoint>"
        return urlunsplit(
            (parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")
        )
    except ValueError:
        return "<invalid endpoint>"


def response_headers(response: Any) -> dict[str, str]:
    """Read requests/curl_cffi response headers case-insensitively."""
    headers = getattr(response, "headers", None) or {}
    return {str(key).lower(): str(value).strip() for key, value in headers.items()}


def _request_id(headers: dict[str, str]) -> str:
    for name in ("x-vercel-id", "cf-ray", "x-request-id"):
        value = headers.get(name, "")
        # IDs are useful to provider support; reject arbitrary text or headers
        # with control characters rather than logging a server-provided payload.
        if re.fullmatch(r"[A-Za-z0-9._|:-]{1,200}", value):
            return f"; request-id={value}"
    return ""


def access_block_detail(response: Any, url: str) -> str | None:
    """Return an access-denial diagnostic, or None for a different failure."""
    status = response.status_code
    if status < 400:
        return None
    headers = response_headers(response)
    body = getattr(response, "text", "")
    body = body.lower() if isinstance(body, str) else ""
    mitigation = headers.get("x-vercel-mitigated", "").lower()
    html = "text/html" in headers.get("content-type", "").lower() or any(
        marker in body for marker in ("<html", "<!doctype html", "<title")
    )
    if mitigation in ("challenge", "deny", "denied") or (
        html and "vercel security checkpoint" in body
    ):
        evidence = (
            f"; x-vercel-mitigated={mitigation}"
            if mitigation in ("challenge", "deny", "denied")
            else ""
        )
        block_name = (
            "Vercel firewall" if mitigation in ("deny", "denied")
            else "Vercel Security Checkpoint"
        )
        return (
            f"{safe_endpoint(url)} access blocked by {block_name} "
            f"(HTTP {status}{evidence}){_request_id(headers)}"
        )
    if status in (403, 503) and ("cloudflare" in body or "cf-ray" in headers):
        return (
            f"{safe_endpoint(url)} access blocked by Cloudflare "
            f"(HTTP {status}){_request_id(headers)}"
        )
    if status in (401, 403):
        return f"{safe_endpoint(url)} access denied (HTTP {status}){_request_id(headers)}"
    return None


def _http_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OverflowError):
        return None


def retry_after_seconds(response: Any) -> float | None:
    """Parse standard Retry-After delta-seconds or an HTTP date, without sleeping."""
    headers = response_headers(response)
    value = headers.get("retry-after", "")
    if not value:
        return None
    if re.fullmatch(r"[0-9]+", value):
        try:
            seconds = float(value)
        except (ValueError, OverflowError):
            return None
        return seconds if math.isfinite(seconds) else None
    retry_date = _http_date(value)
    if retry_date is None:
        return None
    baseline = _http_date(headers.get("date", "")) or datetime.now(timezone.utc)
    seconds = (retry_date - baseline).total_seconds()
    return float(max(0, math.ceil(seconds))) if math.isfinite(seconds) else None


def rate_limit_detail(response: Any, url: str) -> str:
    """Describe a real 429 without including an untrusted response body."""
    detail = f"{safe_endpoint(url)} rate limited (HTTP 429)"
    seconds = retry_after_seconds(response)
    if seconds is not None:
        detail += f"; retry-after={math.ceil(seconds)}s"
    return detail + _request_id(response_headers(response))
