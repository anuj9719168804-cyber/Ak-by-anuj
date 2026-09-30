import logging
import time
from collections import defaultdict, deque
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import config
from terabox_resolver import is_terabox_link, resolve_terabox
import downloader

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
async def limiter(request: Request, call_next):
    if request.url.path in ("/health", "/"):
        return await call_next(request)
    try:
        _rate_limit(_client_ip(request))
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": False, "error": e.detail})
    return await call_next(request)

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

async def _resolve(url: str):
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
        return {"status": True, "creator": "Ak", "data": data}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": False, "error": str(e)})
    except Exception as e:
        logger.exception("TeraBox resolve failed")
        return JSONResponse(status_code=502, content={"status": False, "error": str(e)})

@app.get("/api/terabox")
async def terabox(url: str = Query(..., description="TeraBox share URL")):
    return await _resolve(url)

@app.get("/api")
async def legacy(url: str = Query(..., description="TeraBox share URL")):
    return await _resolve(url)

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
