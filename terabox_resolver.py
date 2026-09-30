"""TeraBox share resolver.

Flow: open share page (jsToken) -> /api/shorturlinfo (shareid, uk, sign, timestamp)
-> /share/list (paginated, folders walked recursively) -> optional /share/download
(needs TERABOX_COOKIE) for direct links. Every JSON/page call falls back to
FlareSolverr when Cloudflare blocks plain requests. Nothing is hard-coded from
captured sessions.
"""
import html
import json
import logging
import re
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

import config
import flaresolverr_client as fs

logger = logging.getLogger("terabox_resolver")

DOMAINS = {
    "terabox.app", "terabox.com", "teraboxshare.com", "teraboxlink.com",
    "terasharefile.com", "terafileshare.com", "terasharelink.com",
    "1024terabox.com", "1024tera.com", "nephobox.com", "4funbox.com",
    "momerybox.com", "teraboxapp.com", "mirrobox.com", "freeterabox.com",
    "tibibox.com", "gcloud.tera.com", "gibibox.com", "goaibox.com",
    "terabox.club", "dm.terabox.com", "dubox.com",
}
DOMAINS |= {d.strip().lower().lstrip(".") for d in config.TERABOX_EXTRA_DOMAINS.split(",") if d.strip()}
API_HOSTS = ["www.terabox.com", "dm.terabox.com", "www.1024tera.com", "www.nephobox.com"]
APP_ID = "250528"
MAX_DIR_DEPTH = 3
MAX_FILES = 200
MAX_DOWNLOAD_LOOKUPS = 25
CACHE_TTL = 300

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

ERRNO_MESSAGES = {
    -9: "Share not found, removed, or it needs an extraction password",
    -6: "TeraBox asked for login (set TERABOX_COOKIE)",
    105: "Invalid share link",
    145: "Share link expired or cancelled",
    400141: "TeraBox requires verification for this share",
    4000020: "TeraBox requires verification for this share",
}

_cache = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- url helpers
def _host(url):
    return (urlparse(url).hostname or "").lower()


def is_terabox_link(url: str) -> bool:
    try:
        p = urlparse(url)
        h = (p.hostname or "").lower()
        return p.scheme in {"http", "https"} and bool(h) and any(
            h == d or h.endswith("." + d) for d in DOMAINS
        )
    except Exception:
        return False


def extract_surl(url: str, final_url: str | None = None) -> str:
    for raw in (url, final_url or ""):
        if not raw:
            continue
        p = urlparse(raw)
        q = parse_qs(p.query)
        for key in ("surl", "shorturl"):
            if q.get(key):
                return q[key][0].strip()
        m = re.search(r"/s/([^/?#]+)", p.path)
        if m:
            return unquote(m.group(1)).strip()
    raise ValueError("Could not extract TeraBox share code (surl) from URL")


# ------------------------------------------------------------ session / misc
def _cookies():
    out = {}
    for part in (config.TERABOX_COOKIE or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k:
                out[k] = v
    return out


def _fs_cookies(host):
    return [{"name": k, "value": v, "domain": host, "path": "/"} for k, v in _cookies().items()]


def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    c = _cookies()
    if c:
        s.cookies.update(c)
    return s


def _extract_jstoken(text: str) -> str:
    patterns = [
        r"fn%28%22([0-9A-Fa-f]+)%22%29",
        r"fn\(\s*[\"']([0-9A-Fa-f]{16,})[\"']\s*\)",
        r"window\\?\.jsToken\s*=\s*[\"']([^\"']+)",
        r"window\.jsToken\s*=\s*JSON\.parse\([\"']([^\"']+)",
        r"\"jsToken\"\s*:\s*\"([^\"]+)\"",
        r"\\\"jsToken\\\"\s*:\s*\\\"([^\"\\]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text or "", re.I)
        if m:
            return html.unescape(m.group(1))
    return ""


def _format_size(n):
    try:
        size = float(int(n))
    except Exception:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return "Unknown"


def _thumbs(item):
    t = item.get("thumbs") or {}
    return [u for u in (t.get("url3"), t.get("url2"), t.get("url1"), t.get("icon")) if u]


# ------------------------------------------------------------------ network
def _open_page(session, url):
    """Plain request first, FlareSolverr if blocked. -> (final_url, html, used_fs)."""
    blocked = False
    try:
        r = session.get(url, allow_redirects=True, timeout=config.REQUEST_TIMEOUT)
        if not fs.looks_blocked(r.status_code, r.text):
            return r.url, r.text, False
        blocked = True
        logger.info("Direct page fetch looks blocked (HTTP %s)", r.status_code)
    except requests.RequestException as e:
        if not fs.enabled():
            raise
        logger.info("Direct page fetch failed (%s), trying FlareSolverr", e)
    if not fs.enabled():
        if blocked:
            raise RuntimeError("Cloudflare blocked the request. Set FLARESOLVERR_URL to enable the bypass.")
        raise RuntimeError("Unable to open TeraBox share page")
    sol = fs.get(url, cookies=_fs_cookies(_host(url)) or None)
    return sol.get("url") or url, sol.get("response") or "", True


def _fetch_json(session, host, path, params, referer, prefer_fs, data=None):
    """GET (or form POST when `data` is given) JSON: plain requests unless blocked, FlareSolverr fallback."""
    endpoint = f"https://{host}{path}"
    if not prefer_fs:
        try:
            if data is None:
                r = session.get(endpoint, params=params, headers={"Referer": referer}, timeout=config.REQUEST_TIMEOUT)
            else:
                r = session.post(endpoint, params=params, data=data, headers={"Referer": referer}, timeout=config.REQUEST_TIMEOUT)
            if not fs.looks_blocked(r.status_code, r.text):
                if r.status_code == 200:
                    return r.json()
                return {"errno": -1, "errmsg": f"HTTP {r.status_code}"}
        except (requests.RequestException, ValueError) as e:
            if not fs.enabled():
                return {"errno": -1, "errmsg": str(e)}
    if not fs.enabled():
        return {"errno": -1, "errmsg": "Cloudflare blocked the request (FLARESOLVERR_URL not set)"}
    full = requests.Request("GET", endpoint, params=params).prepare().url
    cks = _fs_cookies(host) or None
    sol = fs.get(full, cookies=cks) if data is None else fs.post(full, data, cookies=cks)
    return fs.json_from_solution(sol)


class Tok(str):
    """jsToken string that also carries the page's pcftoken."""
    pcf = ""


def _extract_pcftoken(text: str) -> str:
    m = re.search(r"pcftoken[\"']?\s*[:=]\s*[\"']([0-9a-f]+)", text or "")
    return m.group(1) if m else "0"


def _base_params(js_token):
    p = {"app_id": APP_ID, "web": "1", "channel": "dubox", "clienttype": "0"}
    if js_token:
        p["jsToken"] = str(js_token)
    pcf = getattr(js_token, "pcf", "")
    if pcf and pcf != "0":
        p["pcftoken"] = pcf
    return p


def _codes(surl, stripped_first):
    """Share code variants. Some endpoints want the code without its leading '1'."""
    short = surl[1:] if surl.startswith("1") and len(surl) > 20 else surl
    full = surl if surl.startswith("1") else "1" + surl
    order = [short, full] if stripped_first else [full, short]
    return list(dict.fromkeys(order))


def _hosts(base_domain):
    out = []
    for h in [base_domain] + API_HOSTS:
        if h and h not in out:
            out.append(h)
    return out


def _errmsg(data):
    if not isinstance(data, dict):
        return "Unexpected response"
    errno = data.get("errno")
    try:
        errno_i = int(errno)
    except (TypeError, ValueError):
        errno_i = None
    return ERRNO_MESSAGES.get(errno_i) or data.get("errmsg") or data.get("error") or (f"errno {errno}" if errno else "No files found")


# --------------------------------------------------------------- share calls
def _share_info(session, hosts, surl, js_token, prefer_fs):
    """shareid / uk / sign / timestamp (+ sometimes the root list)."""
    for host in hosts:
        for code in _codes(surl, stripped_first=False):
            try:
                params = {**_base_params(js_token), "shorturl": code, "root": "1", "page": "1", "num": "20"}
                data = _fetch_json(session, host, "/api/shorturlinfo", params,
                                   f"https://{host}/sharing/link?surl={quote(code)}", prefer_fs)
                if isinstance(data, dict) and str(data.get("errno", 0)) == "0" and (data.get("shareid") or data.get("list")):
                    return data
            except Exception as e:
                logger.info("shorturlinfo via %s failed: %s", host, e)
    return {}


def _list_dir(session, hosts, surl, js_token, prefer_fs, directory=None):
    """All items of the share root (or of `directory`), following pages. -> (items, err, _)"""
    last_err = None
    for host in hosts:
        for code in _codes(surl, stripped_first=True):
            items, page, ok = [], 1, False
            while len(items) < MAX_FILES:
                params = {**_base_params(js_token), "shorturl": code, "page": str(page),
                          "num": "100", "by": "name", "order": "asc", "scene": ""}
                if directory:
                    params["dir"] = directory
                else:
                    params["root"] = "1"
                try:
                    data = _fetch_json(session, host, "/share/list", params,
                                       f"https://{host}/sharing/link?surl={quote(code)}", prefer_fs)
                except Exception as e:
                    last_err = {"errno": -1, "errmsg": str(e)}
                    break
                if not isinstance(data, dict) or str(data.get("errno", 0)) != "0":
                    last_err = data if isinstance(data, dict) else {"errno": -1, "errmsg": "bad response"}
                    break
                batch = [x for x in (data.get("list") or []) if isinstance(x, dict)]
                ok = True
                items.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            if ok and items:
                return items, None, {}
            if ok:
                last_err = last_err or {"errno": 0, "errmsg": "Empty list"}
    return [], last_err, {}


def _walk(session, hosts, surl, js_token, prefer_fs):
    """Root listing, then folders (depth-limited). Returns (files, error)."""
    root, err, _ = _list_dir(session, hosts, surl, js_token, prefer_fs)
    if err and not root:
        return [], err
    files, queue = [], [(x, 0) for x in root]
    while queue and len(files) < MAX_FILES:
        item, depth = queue.pop(0)
        if str(item.get("isdir", "0")) == "1":
            if depth < MAX_DIR_DEPTH and item.get("path"):
                sub, _, _ = _list_dir(session, hosts, surl, js_token, prefer_fs, directory=item["path"])
                queue.extend((x, depth + 1) for x in sub)
            continue
        files.append(item)
    return files, None


def _get_dlinks(session, hosts, info, files, js_token, prefer_fs):
    """Fill item['dlink'] via POST /share/download. Works only with a logged-in cookie."""
    if not _cookies() or not (info.get("shareid") and info.get("uk") and info.get("sign")):
        return
    for item in files[:MAX_DOWNLOAD_LOOKUPS]:
        if item.get("dlink") or not item.get("fs_id"):
            continue
        params = {**_base_params(js_token), "sign": info["sign"], "timestamp": str(info.get("timestamp", int(time.time())))}
        form = {
            "shareid": str(info["shareid"]), "uk": str(info["uk"]), "product": "share",
            "fid_list": f"[{item['fs_id']}]", "primaryid": str(info["shareid"]), "type": "nolimit",
        }
        for host in hosts[:2]:
            try:
                data = _fetch_json(session, host, "/share/download", params, f"https://{host}/", prefer_fs, data=form)
                if isinstance(data, dict) and str(data.get("errno", 0)) == "0" and data.get("dlink"):
                    item["dlink"] = data["dlink"]
                    break
            except Exception as e:
                logger.info("share/download via %s failed: %s", host, e)
        time.sleep(0.3)


STREAM_TYPES = ["M3U8_AUTO_1080", "M3U8_AUTO_720", "M3U8_AUTO_480", "M3U8_AUTO_360"]
MAX_STREAM_LOOKUPS = 5


def _fetch_text(session, host, path, params, referer, prefer_fs):
    """GET raw text (m3u8). Returns (url, text). Plain first, FlareSolverr fallback."""
    endpoint = f"https://{host}{path}"
    full = requests.Request("GET", endpoint, params=params).prepare().url
    if not prefer_fs:
        try:
            r = session.get(full, headers={"Referer": referer}, timeout=config.REQUEST_TIMEOUT)
            if not fs.looks_blocked(r.status_code, r.text):
                return full, r.text if r.status_code == 200 else ""
        except requests.RequestException:
            pass
    if not fs.enabled():
        return full, ""
    sol = fs.get(full, cookies=_fs_cookies(host) or None)
    body = sol.get("response") or ""
    m = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.S | re.I)
    return full, html.unescape(m.group(1)) if m else re.sub(r"<[^>]+>", "", body)


def _get_streams(session, hosts, info, raw_files, js_token, prefer_fs):
    """Cookie-less path: the guest web player streams shared videos as HLS.

    Returns {fs_id: {"url":..., "quality":...}} for videos where TeraBox served a playlist.
    """
    out = {}
    if not (info.get("shareid") and info.get("uk") and info.get("sign")):
        return out
    videos = [x for x in raw_files
              if x.get("fs_id") and not x.get("dlink") and
              (str(x.get("category")) == "1" or (x.get("server_filename") or "").lower().endswith(VIDEO_EXT))]
    for item in videos[:MAX_STREAM_LOOKUPS]:
        for host in hosts[:2]:
            found = False
            for typ in STREAM_TYPES:
                params = {
                    **_base_params(js_token), "uk": str(info["uk"]), "shareid": str(info["shareid"]),
                    "fid": str(item["fs_id"]), "sign": info["sign"],
                    "timestamp": str(info.get("timestamp", int(time.time()))), "type": typ,
                }
                try:
                    url, text = _fetch_text(session, host, "/share/streaming", params, f"https://{host}/", prefer_fs)
                except Exception as e:
                    logger.info("streaming %s via %s failed: %s", typ, host, e)
                    continue
                if "#EXTM3U" in (text or "")[:200]:
                    out[str(item["fs_id"])] = {"url": url, "quality": typ.split("_")[-1] + "p"}
                    found = True
                    break
            if found:
                break
    return out


def _extract_pwd(url):
    return (parse_qs(urlparse(url).query).get("pwd") or [""])[0].strip()


def _verify_password(session, host, surl, pwd, js_token, prefer_fs):
    code = _codes(surl, stripped_first=True)[0]
    data = _fetch_json(session, host, "/share/verify", {**_base_params(js_token), "surl": code},
                       f"https://{host}/sharing/link?surl={quote(code)}", prefer_fs, data={"pwd": pwd})
    if not isinstance(data, dict) or str(data.get("errno", 0)) != "0":
        errno = data.get("errno") if isinstance(data, dict) else -1
        raise RuntimeError(f"Share password verification failed (errno={errno})")


def _third_party_url():
    u = (config.TERABOX_THIRD_PARTY_URL or "").strip()
    return "" if u.lower() in ("", "off", "0", "none", "false") else u


def _third_party(url):
    """Cookie-less fallback via an external resolver. -> list of raw items with dlink."""
    api = _third_party_url()
    if not api:
        return []
    host = urlparse(api)
    try:
        r = requests.post(api, json={"url": url}, timeout=config.REQUEST_TIMEOUT,
                          headers={"Referer": f"{host.scheme}://{host.netloc}/", "User-Agent": UA})
        data = r.json()
    except Exception as e:
        logger.info("Third-party resolver failed: %s", e)
        return []
    if not isinstance(data, dict) or str(data.get("errno")) != "0":
        return []
    out = []
    for x in data.get("list") or []:
        if isinstance(x, dict) and x.get("direct_link"):
            out.append({"server_filename": x.get("server_filename") or "Unknown", "size": x.get("size", 0),
                        "path": x.get("path", ""), "fs_id": x.get("fs_id", ""), "dlink": x["direct_link"],
                        "_third_party": True})
    return out


def _call_proxy(session, surl):
    if not config.TERABOX_PROXY_URL:
        return None
    r = session.get(config.TERABOX_PROXY_URL, params={"mode": "resolve", "surl": surl, "raw": "1"}, timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("upstream", data.get("data", data))


def _direct_from_dlink(session, dlink):
    if not dlink:
        return ""
    try:
        r = session.head(dlink, allow_redirects=False, timeout=config.REQUEST_TIMEOUT)
        return r.headers.get("Location") or dlink
    except requests.RequestException:
        return dlink


VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".flv")


def _normalize_file(item, session):
    name = item.get("server_filename") or item.get("filename") or "Unknown"
    try:
        size_int = int(item.get("size", 0))
    except Exception:
        size_int = 0
    dlink = item.get("dlink") or item.get("download_url") or item.get("download_link") or ""
    third = bool(item.get("_third_party"))
    direct = dlink if third else (_direct_from_dlink(session, dlink) if dlink else "")
    thumbs = _thumbs(item)
    return {
        "filename": name,
        "name": name,
        "size": _format_size(size_int),
        "size_bytes": size_int,
        "extension": name.rsplit(".", 1)[-1].lower() if "." in name else "",
        "category": "Video" if str(item.get("category")) == "1" or name.lower().endswith(VIDEO_EXT) else "File",
        "thumbnail": thumbs[0] if thumbs else "",
        "thumbnails": thumbs,
        "download_url": direct or dlink,
        "direct_url": direct or dlink,
        "path": item.get("path", ""),
        "fs_id": str(item.get("fs_id", "")),
        "duration_seconds": item.get("duration"),
        "width": item.get("width"),
        "height": item.get("height"),
        "is_directory": False,
        "source": "third_party" if third else "official",
    }


# ------------------------------------------------------------------- public
def _resolve(url: str) -> dict:
    session = _session()
    try:
        final_url, page_text, via_fs = _open_page(session, url)
        surl = extract_surl(url, final_url)
        js_token = Tok(_extract_jstoken(page_text))
        js_token.pcf = _extract_pcftoken(page_text)
        base_domain = _host(final_url) or "www.terabox.com"
    except (requests.RequestException, RuntimeError, ValueError) as e:
        raise RuntimeError(f"Unable to open TeraBox share page: {e}")

    hosts = _hosts(base_domain)
    pwd = _extract_pwd(url)
    if pwd:
        try:
            _verify_password(session, hosts[0], surl, pwd, js_token, via_fs)
        except RuntimeError as e:
            return {"error": str(e), "errno": -12, "surl": surl}
    info = _share_info(session, hosts, surl, js_token, via_fs)
    raw_files, err = _walk(session, hosts, surl, js_token, via_fs)

    if not raw_files and info.get("list"):  # shorturlinfo already carried the list
        raw_files = [x for x in info["list"] if isinstance(x, dict) and str(x.get("isdir", "0")) != "1"]

    if not raw_files:
        try:
            proxy_data = _call_proxy(session, surl)
        except Exception as e:
            logger.info("Proxy fallback failed: %s", e)
            proxy_data = None
        if proxy_data and proxy_data.get("list"):
            raw_files = [x for x in proxy_data["list"] if isinstance(x, dict)]
        else:
            raw_files = _third_party(url)
            if not raw_files:
                errdata = err or info or {}
                return {"error": _errmsg(errdata), "errno": errdata.get("errno", -1), "surl": surl}

    _get_dlinks(session, hosts, info, raw_files, js_token, via_fs)
    if not _cookies() and any(not x.get("dlink") for x in raw_files):
        tp = {x["server_filename"]: x for x in _third_party(url)}
        for x in raw_files:
            hit = tp.get(x.get("server_filename"))
            if hit and not x.get("dlink"):
                x["dlink"], x["_third_party"] = hit["dlink"], True
    streams = _get_streams(session, hosts, info, raw_files, js_token, via_fs)
    files = [_normalize_file(x, session) for x in raw_files]
    for f in files:
        st = streams.get(f["fs_id"])
        if st:
            f["stream_url"], f["stream_quality"] = st["url"], st["quality"]
    first = files[0] if files else {}
    title = info.get("title") or first.get("filename") or "TeraBox file"
    thumbs = [f["thumbnail"] for f in files if f.get("thumbnail")]
    links = [{"title": f["filename"], "url": f["download_url"]} for f in files if f.get("download_url")]
    m3u8 = [{"title": f["filename"], "url": f["stream_url"], "quality": f["stream_quality"]}
            for f in files if f.get("stream_url")]

    return {
        "title": title,
        "files": files,
        "file_count": len(files),
        "links": links,
        "m3u8_links": m3u8,
        "stream_url": m3u8[0]["url"] if m3u8 else "",
        "download_url": first.get("download_url", ""),
        "download_note": "" if links else (
            "No direct download link found. Use stream_url / m3u8_links for playback."
            if m3u8 else
            "TeraBox exposed no download or stream link without login for this share. Optional: set TERABOX_COOKIE."),
        "thumbnail": first.get("thumbnail", ""),
        "size": first.get("size", "Unknown"),
        "size_bytes": first.get("size_bytes", 0),
        "extension": first.get("extension", ""),
        "category": first.get("category", "File"),
        "videoDetails": {
            "title": title,
            "lengthSeconds": first.get("duration_seconds"),
            "thumbnails": [{"url": u} for u in thumbs],
            "width": first.get("width"),
            "height": first.get("height"),
        },
        "download_headers": ({"Cookie": "; ".join(f"{k}={v}" for k, v in _cookies().items()),
                              "User-Agent": UA, "Referer": "https://www.terabox.com/"}
                             if _cookies() else {}),
        "source_url": url,
        "surl": surl,
    }


def resolve_terabox(url: str) -> dict:
    if not is_terabox_link(url):
        raise ValueError("Invalid TeraBox share URL")
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    data = _resolve(url)
    if not data.get("error"):
        with _cache_lock:
            if len(_cache) > 200:
                _cache.clear()
            _cache[url] = (now, data)
    return data
