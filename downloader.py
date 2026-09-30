"""Streams a resolved TeraBox file through this server (Range/resume supported)."""
import re
from urllib.parse import quote

import requests

import config

CHUNK = 256 * 1024
PASS_HEADERS = ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type")


def pick_file(data: dict, index: int = 0, name: str = ""):
    files = [f for f in data.get("files", []) if f.get("download_url")]
    if not files:
        raise LookupError(data.get("download_note") or "No downloadable file in this share")
    if name:
        for f in files:
            if f["filename"] == name:
                return f
        raise LookupError(f"File not found in share: {name}")
    if not 0 <= index < len(files):
        raise LookupError(f"index out of range (0-{len(files) - 1})")
    return files[index]


def upstream_headers(entry: dict, data: dict, range_header: str | None) -> dict:
    h = {}
    if entry.get("source") != "third_party":
        h.update(data.get("download_headers") or {})  # Cookie/UA/Referer needed by official dlinks
    h.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
    if range_header and re.fullmatch(r"bytes=\d*-\d*(,\s*\d*-\d*)*", range_header.strip()):
        h["Range"] = range_header.strip()
    return h


def open_upstream(entry: dict, data: dict, range_header: str | None = None):
    """Returns (status_code, response_headers, byte_iterator). Caller must exhaust or close the iterator."""
    r = requests.get(entry["download_url"], headers=upstream_headers(entry, data, range_header),
                     stream=True, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
    if r.status_code not in (200, 206):
        r.close()
        raise RuntimeError(f"Upstream returned HTTP {r.status_code} (link expired or needs login)")
    headers = {k: r.headers[k] for k in PASS_HEADERS if k in r.headers}
    headers.setdefault("Accept-Ranges", "bytes")
    fname = entry["filename"]
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\\\]', "_", fname)
    headers["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}"

    def gen():
        try:
            for chunk in r.iter_content(CHUNK):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return r.status_code, headers, gen()
