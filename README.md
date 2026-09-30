# 🚀 Ak TeraBox Resolver API

Standalone FastAPI API for TeraBox share links, built in the same simple style as the Ak xHamster resolver project.

## Endpoints

- `GET /` — API information
- `GET /health` — health check
- `GET /api/terabox?url=<TERABOX_URL>` — resolve TeraBox share
- `GET /api?url=<TERABOX_URL>` — compatibility endpoint
- `GET /docs` — Swagger UI

## Example

```text
https://YOUR-RENDER-URL.onrender.com/api/terabox?url=https://1024terabox.com/s/XXXXXXXX
```

The response includes filename, size, extension, thumbnail, fs_id, metadata and a direct/download URL when TeraBox exposes one to the resolver.

## Supported share domains

`terabox.app`, `terabox.com`, `1024terabox.com`, `1024tera.com`, `terasharefile.com`, `terasharelink.com`, `terafileshare.com`, `teraboxshare.com`, `teraboxlink.com`, `nephobox.com` and related subdomains.

## Important

TeraBox links and tokens can expire or require verification. The API does not hard-code the session/token values from a captured HAR. For shares that require authentication/verification, set `TERABOX_COOKIE` or configure an authorized resolver fallback using `TERABOX_PROXY_URL`.

Do not expose private cookies publicly or commit them to GitHub.

## Render

Use Docker deployment or the Python service with:

```text
Build: pip install -r requirements.txt
Start: uvicorn app:app --host 0.0.0.0 --port $PORT
```

Python 3.11 is recommended.

## Cloudflare bypass (FlareSolverr)

When TeraBox answers with a Cloudflare challenge (HTTP 403/429/503 or a "Just a moment" page), the
resolver automatically retries the share page **and** the `/share/list` call through
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (patched build). Set:

| Env var | Meaning | Default |
|---|---|---|
| `FLARESOLVERR_URL` | e.g. `http://flaresolverr:8191/v1` (empty = disabled) | empty |
| `FLARESOLVERR_TIMEOUT_MS` | max solve time | `60000` |
| `FLARESOLVERR_SESSION` | reused browser session name (no re-solve per call) | `ak-terabox` |
| `FLARESOLVERR_SESSION_TTL_MIN` | session lifetime | `30` |
| `FLARESOLVERR_PROXY` | optional proxy for Chromium (residential IP passes challenges more often) | empty |

Run both together: `docker compose up -d --build` (copy `flaresolver/FlareSolverr` from the patch repo to `./FlareSolverr` first).

Note: `cf_clearance` is tied to IP + User-Agent, so JSON calls are fetched *through* FlareSolverr
instead of replaying its cookies from another machine. Render free tier cannot run Chromium; host
FlareSolverr on a VPS and point `FLARESOLVERR_URL` to it.

Offline test: `python test_flare_fallback.py`

## Without a cookie

`TERABOX_COOKIE` is optional. Without it the API returns file metadata, sizes, thumbnails and, for
videos, an HLS playlist link (`stream_url` / `m3u8_links`) from the guest web player. Plain-file
direct download links need a logged-in cookie on TeraBox's side; without one `download_url` stays empty.
Stream links can be tied to the IP that fetched them, so play them from the same network as the API/FlareSolverr
if a player gets 403.

## Cookie-less direct links (third-party fallback)

Without a cookie TeraBox will not hand out plain-file download links itself. As a fallback the API
asks an external resolver (`TERABOX_THIRD_PARTY_URL`, default `https://teraboxdl.site/api/proxy`, the same
one mirror-leech-telegram-bot uses). The share URL is sent to that service, it can disappear or change
any time, and files fetched this way carry `"source": "third_party"`. Set `TERABOX_THIRD_PARTY_URL=off`
to disable. Password-protected shares work with `?pwd=CODE` on the link.

## Download endpoint

`GET /api/terabox/download?url=<SHARE_URL>&index=0` (or `&name=<exact filename>`) resolves the share and
streams the file through this server. Range/resume works. `index` counts only files that have a direct link.
HLS-only videos (`stream_url`) are not downloadable this way; open the m3u8 in a player or use ffmpeg.
It uses your server's bandwidth (free hosts often cap it), and the API rate limit applies.

## Domains

All TeraBox mirror domains (and their subdomains) listed in `terabox_resolver.DOMAINS` are accepted.
TeraBox adds new mirrors from time to time; add them without editing code via
`TERABOX_EXTRA_DOMAINS=newmirror.com,other.net`. Only listed domains are fetched (prevents SSRF).
