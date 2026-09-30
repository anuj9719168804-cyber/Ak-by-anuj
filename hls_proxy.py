"""HLS proxy: TeraBox playlists/segments only work with the session (cookies, IP, UA) that
fetched them, so the browser is pointed at this server, which replays that session.

Segment URLs are HMAC-signed so this cannot be used as an open proxy.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from urllib.parse import urljoin

import requests

import config

SECRET = (os.getenv("HLS_SECRET") or "").encode() or os.urandom(32)
CTX_TTL = 3 * 3600
_ctx = {}
_lock = threading.Lock()
_URI_ATTR = re.compile(r'URI="([^"]+)"')


def register_ctx(cookies: dict, ua: str) -> str:
    now = time.time()
    with _lock:
        for k in [k for k, v in _ctx.items() if v["exp"] < now]:
            _ctx.pop(k, None)
        if len(_ctx) > 500:
            _ctx.clear()
        cid = os.urandom(8).hex()
        _ctx[cid] = {"cookies": dict(cookies or {}), "ua": ua, "exp": now + CTX_TTL}
    return cid


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET, payload, hashlib.sha256).hexdigest()[:32]


def make_token(url: str, cid: str) -> str:
    raw = base64.urlsafe_b64encode(json.dumps({"u": url, "c": cid}).encode())
    return f"{raw.decode()}.{_sign(raw)}"


def read_token(token: str):
    try:
        raw, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(raw.encode())):
            raise ValueError
        d = json.loads(base64.urlsafe_b64decode(raw.encode()))
        return d["u"], d["c"]
    except Exception:
        raise PermissionError("Invalid or tampered stream token")


def rewrite_playlist(text: str, base_url: str, cid: str, prefix: str) -> str:
    """Point every segment / key / sub-playlist at `prefix?u=<signed token>`."""
    def wrap(u):
        return f"{prefix}?u={make_token(urljoin(base_url, u.strip()), cid)}"

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            out.append(_URI_ATTR.sub(lambda m: f'URI="{wrap(m.group(1))}"', line))
        else:
            out.append(wrap(s))
    return "\n".join(out) + "\n"


def fetch(token: str, prefix: str, range_header: str | None = None):
    """-> (status, headers, body) where body is bytes (playlist) or an iterator (media)."""
    url, cid = read_token(token)
    with _lock:
        c = _ctx.get(cid)
    if not c:
        raise LookupError("Stream session expired, request the stream link again")
    headers = {"User-Agent": c["ua"] or "Mozilla/5.0", "Referer": "https://www.terabox.com/"}
    if range_header and re.fullmatch(r"bytes=\d*-\d*", range_header.strip()):
        headers["Range"] = range_header.strip()
    r = requests.get(url, headers=headers, cookies=c["cookies"], stream=True,
                     timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
    if r.status_code not in (200, 206):
        r.close()
        raise RuntimeError(f"Upstream returned HTTP {r.status_code}")
    head = next(r.iter_content(512), b"")
    if head.lstrip().startswith(b"#EXTM3U"):
        text = (head + r.content).decode("utf-8", "replace")
        r.close()
        return 200, {"Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "no-store"}, \
            rewrite_playlist(text, url, cid, prefix).encode()
    h = {k: r.headers[k] for k in ("Content-Length", "Content-Range", "Accept-Ranges") if k in r.headers}
    h["Content-Type"] = r.headers.get("Content-Type", "video/mp2t")

    def gen():
        try:
            if head:
                yield head
            for chunk in r.iter_content(256 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return r.status_code, h, gen()


def diagnose(playlist_token: str, prefix: str) -> dict:
    """Fetch playlist + first segment through the proxy path and report where it breaks."""
    rep = {"playlist_ok": False, "segments": 0, "first_segment_status": None,
           "first_segment_bytes": 0, "content_type": None, "error": None}
    try:
        st, _, body = fetch(playlist_token, prefix)
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else ""
        rep["playlist_ok"] = st == 200 and text.startswith("#EXTM3U")
        toks = [l.split("u=", 1)[1] for l in text.splitlines() if l and not l.startswith("#") and "u=" in l]
        rep["segments"] = len(toks)
        if not toks:
            rep["error"] = "Playlist has no segments"
            return rep
        st2, h2, b2 = fetch(toks[0], prefix, "bytes=0-4095")
        rep["first_segment_status"], rep["content_type"] = st2, h2.get("Content-Type")
        if isinstance(b2, (bytes, bytearray)):  # sub-playlist
            rep["error"] = "First entry is another playlist (variant stream)"
        else:
            rep["first_segment_bytes"] = sum(len(c) for c in b2)
    except Exception as e:
        rep["error"] = str(e)
    return rep
