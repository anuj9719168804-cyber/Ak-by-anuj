"""Small FlareSolverr client used as a Cloudflare fallback.

FlareSolverr drives a real (undetected) Chromium, so the page it returns is what
a browser would see. `cf_clearance` is bound to IP + User-Agent, therefore JSON
endpoints are fetched *through* FlareSolverr as well (same browser session, same
IP) instead of replaying cookies from a different machine.
"""
import html as htmllib
import json
import logging
import re

import requests

import config

logger = logging.getLogger("flaresolverr_client")

_CF_MARKERS = (
    "just a moment", "cf-browser-verification", "challenge-platform",
    "cf_chl_opt", "attention required! | cloudflare", "cf-turnstile",
)


def enabled() -> bool:
    return bool(config.FLARESOLVERR_URL)


def looks_blocked(status_code=None, text: str = "") -> bool:
    """True if a plain-requests response looks like a Cloudflare challenge."""
    if status_code in (403, 429, 503):
        return True
    low = (text or "")[:6000].lower()
    return any(m in low for m in _CF_MARKERS)


def _post(payload: dict) -> dict:
    timeout = config.FLARESOLVERR_TIMEOUT_MS / 1000 + 15
    r = requests.post(config.FLARESOLVERR_URL, json=payload, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"FlareSolverr returned non-JSON (HTTP {r.status_code})")
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message') or data}")
    return data


def get(url: str, session: str | None = None, cookies: list | None = None) -> dict:
    """request.get through FlareSolverr. Returns the `solution` dict."""
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": config.FLARESOLVERR_TIMEOUT_MS,
    }
    sess = session if session is not None else config.FLARESOLVERR_SESSION
    if sess:
        payload["session"] = sess
        payload["session_ttl_minutes"] = config.FLARESOLVERR_SESSION_TTL_MIN
    if cookies:
        payload["cookies"] = cookies
    if config.FLARESOLVERR_PROXY:
        payload["proxy"] = {"url": config.FLARESOLVERR_PROXY}
    return _post(payload).get("solution") or {}


def post(url: str, form: dict, session: str | None = None, cookies: list | None = None) -> dict:
    """request.post (form-urlencoded) through FlareSolverr. Returns the `solution` dict."""
    from urllib.parse import urlencode
    payload = {
        "cmd": "request.post",
        "url": url,
        "postData": urlencode(form),
        "maxTimeout": config.FLARESOLVERR_TIMEOUT_MS,
    }
    sess = session if session is not None else config.FLARESOLVERR_SESSION
    if sess:
        payload["session"] = sess
        payload["session_ttl_minutes"] = config.FLARESOLVERR_SESSION_TTL_MIN
    if cookies:
        payload["cookies"] = cookies
    if config.FLARESOLVERR_PROXY:
        payload["proxy"] = {"url": config.FLARESOLVERR_PROXY}
    return _post(payload).get("solution") or {}


def destroy_session(session: str | None = None):
    sess = session or config.FLARESOLVERR_SESSION
    if not sess or not enabled():
        return
    try:
        _post({"cmd": "sessions.destroy", "session": sess})
    except Exception as e:  # session may already be gone
        logger.debug("sessions.destroy: %s", e)


def json_from_solution(solution: dict):
    """Chromium wraps a raw JSON response in <pre>...</pre>; unwrap and parse."""
    body = (solution or {}).get("response") or ""
    m = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.S | re.I)
    raw = htmllib.unescape(m.group(1)) if m else re.sub(r"<[^>]+>", "", body)
    raw = raw.strip()
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("FlareSolverr response was not JSON: " + raw[:160].replace("\n", " "))
