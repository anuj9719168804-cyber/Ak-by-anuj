import logging
import time
from collections import defaultdict, deque
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import config
from terabox_resolver import is_terabox_link, resolve_terabox
import downloader
import hls_proxy
import copy
from fastapi.responses import Response, HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("terabox_api")

app = FastAPI(
    title="Ak TeraBox Resolver API",
    description="Standalone TeraBox share-link resolver API.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"]
)

_hits = defaultdict(deque)
_last_purge = 0.0

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _rate_limit(ip: str):
    global _last_purge
    now = time.time()
    if now - _last_purge > 300:  # drop idle IPs so memory doesn't grow forever
        _last_purge = now
        for k in [k for k, q in _hits.items() if not q or now - q[-1] > 60]:
            _hits.pop(k, None)
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, "Rate limit exceeded, try again later.")
    q.append(now)

@app.middleware("http")
async def normalize_path(request: Request, call_next):
    """/api//player -> /api/player"""
    path = request.scope.get("path", "")
    if "//" in path:
        while "//" in path:
            path = path.replace("//", "/")
        request.scope["path"] = path
        request.scope["raw_path"] = path.encode()
    return await call_next(request)

@app.middleware("http")
async def limiter(request: Request, call_next):
    if request.url.path in ("/health", "/"):
        return await call_next(request)
    try:
        _rate_limit(_client_ip(request))
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": False, "error": e.detail})
    return await call_next(request)

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

@app.get("/", include_in_schema=False)
def root():
    return {
        "status": True,
        "creator": "Ak",
        "message": "TeraBox Resolver API is online",
        "version": "1.0.0",
        "endpoints": {
            "resolve": "/api/terabox?url=<TERABOX_SHARE_URL>",
            "download": "/api/terabox/download?url=<TERABOX_SHARE_URL>&index=0",
            "legacy": "/api?url=<TERABOX_SHARE_URL>",
            "health": "/health",
            "docs": "/docs",
        },
    }

@app.get("/health")
def health():
    return {"status": True, "service": "terabox-api", "provider": "TeraBox",
            "flaresolverr": bool(config.FLARESOLVERR_URL), "cookie_set": bool(config.TERABOX_COOKIE)}

async def _resolve(url: str, base: str = ""):
    if not is_terabox_link(url):
        return JSONResponse(status_code=400, content={
            "status": False,
            "error": "Only TeraBox share links are supported here."
        })
    try:
        data = await run_in_threadpool(resolve_terabox, url)
        if isinstance(data, dict) and data.get("error"):
            code = 404 if data.get("errno") in (-9, 105, 145) else 502
            return JSONResponse(status_code=code, content={"status": False, **data})
        return {"status": True, "creator": "Ak", "data": _public(data, url, base)}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": False, "error": str(e)})
    except Exception as e:
        logger.exception("TeraBox resolve failed")
        return JSONResponse(status_code=502, content={"status": False, "error": str(e)})

def _public(data: dict, share_url: str, base: str) -> dict:
    """API output: hide private keys, point stream links at this server's HLS proxy."""
    # download_headers holds the server's TERABOX_COOKIE: never expose it publicly.
    d = copy.deepcopy({k: v for k, v in data.items() if not k.startswith("_") and k != "download_headers"})
    def mk(name):
        return f"{base}/api/terabox/stream?url={quote(share_url, safe='')}&name={quote(name, safe='')}"
    for f in d.get("files", []):
        if f.get("stream_url"):
            f["stream_url"] = mk(f["filename"])
    for m in d.get("m3u8_links", []):
        m["url"] = mk(m["title"])
    d["stream_url"] = d["m3u8_links"][0]["url"] if d.get("m3u8_links") else ""
    d["player_url"] = (f"{base}/player?url={quote(share_url, safe='')}" if d["stream_url"] else "")
    return d

@app.get("/api/terabox")
async def terabox(request: Request, url: str = Query(..., description="TeraBox share URL")):
    return await _resolve(url, str(request.base_url).rstrip("/"))

@app.get("/api")
async def legacy(request: Request, url: str = Query(..., description="TeraBox share URL")):
    return await _resolve(url, str(request.base_url).rstrip("/"))

@app.get("/api/terabox/stream")
async def stream(request: Request, url: str = Query(...), name: str = Query("")):
    """HLS playlist for a shared video, served through this server (browser has no TeraBox session)."""
    if not is_terabox_link(url):
        return JSONResponse(status_code=400, content={"status": False, "error": "Only TeraBox share links are supported here."})
    try:
        data = await run_in_threadpool(resolve_terabox, url)
        if data.get("error"):
            return JSONResponse(status_code=502, content={"status": False, **data})
        ctxs = data.get("_stream_ctx") or {}
        key = name if name in ctxs else (next(iter(ctxs), ""))
        if not key:
            return JSONResponse(status_code=404, content={"status": False, "error": "No stream available for this share"})
        c = ctxs[key]
        cid = hls_proxy.register_ctx(c.get("cookies"), c.get("ua"))
        prefix = str(request.base_url).rstrip("/") + "/api/terabox/hls"
        token = hls_proxy.make_token(c["url"], cid)
        status, headers, body = await run_in_threadpool(hls_proxy.fetch, token, prefix, None)
    except Exception as e:
        logger.exception("stream failed")
        return JSONResponse(status_code=502, content={"status": False, "error": str(e)})
    return Response(content=body, status_code=status, headers=headers)

PLAYER_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Ak Player</title>
<style>body{margin:0;background:#000;color:#ddd;font:13px/1.4 sans-serif}video{width:100%;max-height:75vh;background:#111}
pre{padding:8px;white-space:pre-wrap;word-break:break-all;margin:0}</style></head><body>
<video id=v controls playsinline></video><pre id=log>loading...</pre>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
const log=t=>{document.getElementById('log').textContent+='\\n'+t};
document.getElementById('log').textContent='';
const v=document.getElementById('v'), src='/api/terabox/stream'+location.search;
if(window.Hls&&Hls.isSupported()){
  const h=new Hls({maxBufferLength:30});
  h.on(Hls.Events.MANIFEST_PARSED,(e,d)=>log('playlist ok, levels: '+d.levels.length));
  h.on(Hls.Events.ERROR,(e,d)=>log((d.fatal?'FATAL ':'warn ')+d.type+' / '+d.details+(d.response?' / HTTP '+d.response.code:'')));
  h.loadSource(src);h.attachMedia(v);
}else if(v.canPlayType('application/vnd.apple.mpegurl')){v.src=src;}
else log('This browser cannot play HLS');
v.addEventListener('error',()=>log('video error code '+(v.error&&v.error.code)));
</script></body></html>"""

@app.get("/player", include_in_schema=False)
@app.get("/api/player", include_in_schema=False)
@app.get("/api/terabox/player", include_in_schema=False)
async def player():
    return HTMLResponse(PLAYER_HTML)

@app.get("/api/terabox/stream/check")
async def stream_check(request: Request, url: str = Query(...), name: str = Query("")):
    """Debug: shows whether playlist and first segment load through the proxy, and the upstream status."""
    if not is_terabox_link(url):
        return JSONResponse(status_code=400, content={"status": False, "error": "Only TeraBox share links are supported here."})
    data = await run_in_threadpool(resolve_terabox, url)
    if data.get("error"):
        return JSONResponse(status_code=502, content={"status": False, **data})
    ctxs = data.get("_stream_ctx") or {}
    key = name if name in ctxs else next(iter(ctxs), "")
    if not key:
        return {"status": False, "error": "No stream found for this share", "note": data.get("download_note")}
    c = ctxs[key]
    cid = hls_proxy.register_ctx(c.get("cookies"), c.get("ua"))
    prefix = str(request.base_url).rstrip("/") + "/api/terabox/hls"
    rep = await run_in_threadpool(hls_proxy.diagnose, hls_proxy.make_token(c["url"], cid), prefix)
    return {"status": True, "file": key, "cookies_captured": len(c.get("cookies") or {}), **rep}

@app.get("/api/terabox/hls", include_in_schema=False)
async def hls(request: Request, u: str = Query(...)):
    prefix = str(request.base_url).rstrip("/") + "/api/terabox/hls"
    try:
        status, headers, body = await run_in_threadpool(hls_proxy.fetch, u, prefix, request.headers.get("range"))
    except PermissionError as e:
        return JSONResponse(status_code=403, content={"status": False, "error": str(e)})
    except LookupError as e:
        return JSONResponse(status_code=410, content={"status": False, "error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=502, content={"status": False, "error": str(e)})
    if isinstance(body, (bytes, bytearray)):
        return Response(content=body, status_code=status, headers=headers)
    return StreamingResponse(body, status_code=status, headers=headers)

@app.get("/api/terabox/download")
async def download(
    request: Request,
    url: str = Query(..., description="TeraBox share URL"),
    index: int = Query(0, description="File number among downloadable files (see /api/terabox)"),
    name: str = Query("", description="Exact filename (overrides index)"),
):
    """Resolve the share and stream the file through this server. Supports Range/resume."""
    if not is_terabox_link(url):
        return JSONResponse(status_code=400, content={"status": False, "error": "Only TeraBox share links are supported here."})
    try:
        data = await run_in_threadpool(resolve_terabox, url)
        if data.get("error"):
            return JSONResponse(status_code=502, content={"status": False, **data})
        entry = downloader.pick_file(data, index, name)
        status, headers, body = await run_in_threadpool(
            downloader.open_upstream, entry, data, request.headers.get("range"))
    except LookupError as e:
        return JSONResponse(status_code=404, content={"status": False, "error": str(e)})
    except Exception as e:
        logger.exception("TeraBox download failed")
        return JSONResponse(status_code=502, content={"status": False, "error": str(e)})
    return StreamingResponse(body, status_code=status, headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, reload=False)
