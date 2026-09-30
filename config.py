import os

PORT = int(os.getenv("PORT", "8000"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "25"))
# Optional cookie string, e.g. ndus=...;csrfToken=...
TERABOX_COOKIE = os.getenv("TERABOX_COOKIE", "").strip()
# Optional fallback gateway. Leave empty for direct TeraBox resolution.
TERABOX_PROXY_URL = os.getenv("TERABOX_PROXY_URL", "").strip()

# --- FlareSolverr (Cloudflare bypass fallback) ---
# e.g. http://flaresolverr:8191/v1 . Leave empty to disable.
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "").strip()
FLARESOLVERR_TIMEOUT_MS = int(os.getenv("FLARESOLVERR_TIMEOUT_MS", "60000"))
# Reusing one named browser session avoids re-solving the challenge on every call.
FLARESOLVERR_SESSION = os.getenv("FLARESOLVERR_SESSION", "ak-terabox").strip()
FLARESOLVERR_SESSION_TTL_MIN = int(os.getenv("FLARESOLVERR_SESSION_TTL_MIN", "30"))
# Optional proxy for Chromium, e.g. socks5://user:pass@host:1080 (residential IP helps a lot)
FLARESOLVERR_PROXY = os.getenv("FLARESOLVERR_PROXY", "").strip()

# Cookie-less fallback: third-party resolver (same one mirror-leech-telegram-bot uses).
# Your share URL is sent to that service. Set to "off" to disable.
TERABOX_THIRD_PARTY_URL = os.getenv("TERABOX_THIRD_PARTY_URL", "https://teraboxdl.site/api/proxy").strip()

# Extra share domains (comma separated), e.g. "newtera.com,teraboxmirror.net"
TERABOX_EXTRA_DOMAINS = os.getenv("TERABOX_EXTRA_DOMAINS", "").strip()

# Second cookie-less fallback (GET ?url=...). Set to "off" to disable.
TERABOX_THIRD_PARTY_URL2 = os.getenv("TERABOX_THIRD_PARTY_URL2", "https://terabox-api-sable.vercel.app/terabox_file_downloadv2").strip()
