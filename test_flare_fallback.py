"""Offline tests (no network): plain block -> FlareSolverr, folders, pagination, dlink, errors, URL parsing."""
import json
import config
config.FLARESOLVERR_URL = "http://flare.test/v1"
config.TERABOX_THIRD_PARTY_URL = "off"
import flaresolverr_client as fs
import terabox_resolver as tr

CF = "<html><title>Just a moment...</title></html>"

class R:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url, self.headers = status, text, url, {}
    def json(self): return json.loads(self.text)

class Blocked:
    headers = {}; cookies = type("C", (), {"update": lambda *a: None})()
    def update(self, *a): pass
    def get(self, url, **kw): return R(403, CF, url)
    def head(self, *a, **kw): raise tr.requests.RequestException("x")

def make_fs(routes):
    calls = []
    def fake(url, session=None, cookies=None):
        calls.append((url, cookies))
        for key, payload in routes:
            if key in url:
                if callable(payload): payload = payload(url)
                if isinstance(payload, str): return {"url": url, "response": payload}
                return {"url": url, "response": "<pre>" + json.dumps(payload) + "</pre>"}
        raise AssertionError("unrouted " + url)
    def fake_post(url, form, session=None, cookies=None):
        calls.append((url + "|POST|" + json.dumps(form), cookies))
        return fake(url, cookies=cookies) if False else _route(routes, url)
    fake.post = fake_post
    return fake, calls

def _route(routes, url):
    for key, payload in routes:
        if key in url:
            if callable(payload): payload = payload(url)
            return {"url": url, "response": "<pre>" + json.dumps(payload) + "</pre>"}
    raise AssertionError("unrouted " + url)

def reset(): tr._cache.clear()

# --- URL parsing
assert tr.is_terabox_link("https://1024terabox.com/s/1abc") and not tr.is_terabox_link("https://evil.com/?terabox.com")
assert not tr.is_terabox_link("not a url") and not tr.is_terabox_link("http:///x")
assert tr.extract_surl("https://terabox.app/wap/share/filelist?surl=1xyz") == "1xyz"
L = "1" + "A" * 21
assert tr._codes(L, True) == ["A" * 21, L] and tr._codes(L, False) == [L, "A" * 21]
assert tr._codes("1abc", True)[0] == "1abc"

# --- 1) blocked -> FS, root file + folder + pagination
def lst(url):
    if "dir=" in url:
        return {"errno": 0, "list": [{"server_filename": "in.mkv", "size": "5", "fs_id": 3, "isdir": "0", "path": "/d/in.mkv"}]}
    if "page=2" in url:
        return {"errno": 0, "list": [{"server_filename": "b.txt", "size": "2", "fs_id": 2, "isdir": "0"}]}
    first = [{"server_filename": "a.mp4", "size": "1048576", "fs_id": 1, "isdir": "0"},
             {"server_filename": "d", "isdir": "1", "path": "/d", "fs_id": 9}]
    first += [{"server_filename": f"x{i}", "size": "1", "fs_id": 100 + i, "isdir": "0"} for i in range(98)]
    return {"errno": 0, "list": first}
fake, calls = make_fs([
    ("shorturlinfo", {"errno": 0, "shareid": 11, "uk": 22, "sign": "s", "timestamp": 1}),
    ("/share/list", lst),
    ("/s/1", '<script>window.jsToken = fn%28%22ABCDEF0123%22%29</script>'),
])
tr._session = lambda: Blocked(); fs.get = fake; fs.post = fake.post; reset()
out = tr.resolve_terabox("https://1024terabox.com/s/1abc")
names = [f["filename"] for f in out["files"]]
assert "a.mp4" in names and "in.mkv" in names and "b.txt" in names, names
assert all(not f["is_directory"] for f in out["files"])
assert any("jsToken=ABCDEF0123" in u for u, _ in calls)
assert out["links"] == [] and out["download_note"]
print("OK 1: block->FS, folders, pagination:", out["file_count"], "files")

# --- 2) cookie -> share/download gives dlink
config.TERABOX_COOKIE = "ndus=abc"
fake, calls = make_fs([
    ("shorturlinfo", {"errno": 0, "shareid": 11, "uk": 22, "sign": "s", "timestamp": 1}),
    ("/share/download", {"errno": 0, "dlink": "https://d.example/f.mp4"}),
    ("/share/list", {"errno": 0, "list": [{"server_filename": "a.mp4", "size": "9", "fs_id": 1, "isdir": "0"}]}),
    ("/s/1", "<html>ok</html>"),
])
fs.get = fake; fs.post = fake.post; reset()
out = tr.resolve_terabox("https://terabox.com/s/1abc")
assert out["download_url"] == "https://d.example/f.mp4" and out["links"], out
assert any(c for _, c in calls), "cookies should be passed to FlareSolverr"
dl = [u for u, _ in calls if "/share/download" in u][0]
assert "|POST|" in dl and '"fid_list": "[1]"' in dl and '"type": "nolimit"' in dl, dl
print("OK 2: dlink via share/download")
config.TERABOX_COOKIE = ""

# --- 3) errors mapped
fake, _ = make_fs([("shorturlinfo", {"errno": 145}), ("/share/list", {"errno": 145}), ("/s/1", "<html/>")])
fs.get = fake; fs.post = fake.post; reset()
out = tr.resolve_terabox("https://terabox.com/s/1gone")
assert out["errno"] == 145 and "expired" in out["error"].lower(), out
print("OK 3: errno mapped ->", out["error"])

# --- 4) cache
fake, calls = make_fs([("shorturlinfo", {"errno": 0}), ("/share/list", {"errno": 0, "list": [{"server_filename": "z.mp4", "size": "1", "fs_id": 1, "isdir": "0"}]}), ("/s/1", "<html/>")])
fs.get = fake; fs.post = fake.post; reset()
tr.resolve_terabox("https://terabox.com/s/1c"); n = len(calls); tr.resolve_terabox("https://terabox.com/s/1c")
assert len(calls) == n; print("OK 4: cache")

# --- 5) FS disabled + blocked -> clear message
config.FLARESOLVERR_URL = ""; reset()
try:
    tr.resolve_terabox("https://terabox.com/s/1abc"); raise SystemExit("should fail")
except RuntimeError as e:
    assert "FLARESOLVERR_URL" in str(e); print("OK 5: clear error without FlareSolverr")

# --- 6) cookie-less: HLS stream for video
config.FLARESOLVERR_URL = "http://flare.test/v1"
def stream(url):
    return "#EXTM3U\n#EXT-X-VERSION:3\n" if "M3U8_AUTO_720" in url else "{\"errno\": 31341}"
fake, calls = make_fs([
    ("shorturlinfo", {"errno": 0, "shareid": 11, "uk": 22, "sign": "s", "timestamp": 1}),
    ("/share/streaming", stream),
    ("/share/list", {"errno": 0, "list": [{"server_filename": "v.mp4", "size": "9", "fs_id": 7, "isdir": "0", "category": 1}, {"server_filename": "n.txt", "size": "1", "fs_id": 8, "isdir": "0"}]}),
    ("/s/1", "<html/>"),
])
tr._session = lambda: Blocked(); fs.get = fake; fs.post = fake.post; reset()
out = tr.resolve_terabox("https://terabox.com/s/1vid")
assert out["m3u8_links"] and out["m3u8_links"][0]["quality"] == "720p", out["m3u8_links"]
assert out["stream_url"] and "fid=7" in out["stream_url"] and "Use stream_url" in out["download_note"]
assert not any("fid=8" in u for u, _ in calls), "non-video must not be probed"
print("OK 6: cookie-less HLS stream:", out["m3u8_links"][0]["quality"])

# --- 7) third-party fallback (no cookie) fills direct links; password verify; short/full codes
config.TERABOX_COOKIE = ""
config.TERABOX_THIRD_PARTY_URL = "https://tp.test/api"
class Resp:
    def __init__(s, d): s._d = d
    def json(s): return s._d
posted = {}
def fake_tp(api, json=None, **kw):
    posted["api"], posted["json"] = api, json
    return Resp({"errno": 0, "list": [{"server_filename": "a.zip", "size": 5, "path": "/a.zip", "direct_link": "https://dl.example/a.zip"}]})
tr.requests.post = fake_tp
codes = []
def lst2(url):
    codes.append(url)
    return {"errno": 0, "list": [{"server_filename": "a.zip", "size": "5", "fs_id": 5, "isdir": "0"}]}
long_code = "1" + "A" * 21
fake, calls = make_fs([
    ("/share/verify", {"errno": 0}),
    ("shorturlinfo", {"errno": 0, "shareid": 1, "uk": 2, "sign": "s", "timestamp": 3}),
    ("/share/list", lst2), ("/s/1", '<html>pcftoken:"ab12"</html>'),
])
tr._session = lambda: Blocked(); fs.get = fake; fs.post = fake.post; reset()
out = tr.resolve_terabox(f"https://terabox.com/s/{long_code}?pwd=1234")
assert out["download_url"] == "https://dl.example/a.zip" and out["files"][0]["source"] == "third_party", out
assert posted["json"]["url"].startswith("https://terabox.com/s/1AAA")
assert any("/share/verify" in u and "1234" in u for u, _ in calls), "password must be verified"
assert any("shorturl=" + "A" * 21 + "&" in u for u, _ in calls), "list should use code without leading 1"
assert any("shorturl=" + long_code + "&" in u for u, _ in calls if "shorturlinfo" in u), "info uses full code"
assert any("pcftoken=ab12" in u for u, _ in calls)
print("OK 7: third-party links, password verify, code variants, pcftoken")

# --- 8) TeraBox fully blocked -> third-party only
fake, calls = make_fs([("shorturlinfo", {"errno": -1}), ("/share/list", {"errno": -1}), ("/s/1", "<html/>")])
fs.get = fake; fs.post = fake.post; reset()
out = tr.resolve_terabox("https://terabox.com/s/1zzz")
assert out["download_url"] == "https://dl.example/a.zip"
print("OK 8: third-party rescues when TeraBox API fails")

# --- 9) downloader: picking, headers, Range passthrough, streaming
import downloader
data = {"files": [{"filename": "a.zip", "download_url": "https://dl.example/a", "source": "official"},
                  {"filename": "नाम.mp4", "download_url": "https://dl.example/b", "source": "third_party"},
                  {"filename": "nolink", "download_url": ""}],
        "download_headers": {"Cookie": "ndus=x", "Referer": "https://www.terabox.com/"}}
assert downloader.pick_file(data, 1)["filename"] == "नाम.mp4" and downloader.pick_file(data, name="a.zip")["filename"] == "a.zip"
try: downloader.pick_file(data, 5); raise SystemExit("should fail")
except LookupError: pass
assert "Cookie" in downloader.upstream_headers(data["files"][0], data, None)
assert "Cookie" not in downloader.upstream_headers(data["files"][1], data, "bytes=0-9"), "no cookie to third-party links"
assert downloader.upstream_headers(data["files"][1], data, "bytes=0-9")["Range"] == "bytes=0-9"
assert "Range" not in downloader.upstream_headers(data["files"][1], data, "evil\r\nX: y")
class Up:
    def __init__(s, code): s.status_code, s.headers, s.closed = code, {"Content-Length": "6", "Content-Range": "bytes 0-5/9"}, False
    def iter_content(s, n): yield b"abc"; yield b"def"
    def close(s): s.closed = True
seen = {}
def fake_get(u, headers=None, **kw): seen.update(u=u, h=headers); return Up(206)
downloader.requests.get = fake_get
code, hdr, it = downloader.open_upstream(data["files"][1], data, "bytes=0-5")
assert code == 206 and b"".join(it) == b"abcdef" and seen["h"]["Range"] == "bytes=0-5"
assert "filename*=UTF-8''" in hdr["Content-Disposition"] and hdr["Content-Range"] == "bytes 0-5/9"
downloader.requests.get = lambda *a, **k: Up(403)
try: downloader.open_upstream(data["files"][0], data); raise SystemExit("should fail")
except RuntimeError as e: assert "403" in str(e)
print("OK 9: downloader (pick, headers, range, streaming, upstream error)")

# --- 10) domains: subdomains ok, lookalikes rejected
for good in ("https://www.terabox.com/s/1a", "https://dm.1024tera.com/s/1a", "https://mirrobox.com/s/1a", "https://x.teraboxapp.com/s/1a", "https://dubox.com/s/1a"):
    assert tr.is_terabox_link(good), good
for bad in ("https://terabox.com.evil.io/s/1a", "https://eviltera-box.com/s/1a", "ftp://terabox.com/s/1a", "https://127.0.0.1/s/1a"):
    assert not tr.is_terabox_link(bad), bad
print("OK 10: domain matching (%d domains)" % len(tr.DOMAINS))

# --- 11) HLS proxy: signed tokens, playlist rewrite, cookie replay
import hls_proxy
cid = hls_proxy.register_ctx({"csrfToken": "abc"}, "UA-X")
tok = hls_proxy.make_token("https://cdn.example/seg1.ts", cid)
assert hls_proxy.read_token(tok) == ("https://cdn.example/seg1.ts", cid)
for bad in (tok[:-2] + "00", "garbage", tok.split(".")[0] + ".deadbeef"):
    try: hls_proxy.read_token(bad); raise SystemExit("tampered token accepted")
    except PermissionError: pass
pl = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:5,\nhttps://cdn.example/a.ts\n#EXTINF:5,\nb.ts\n#EXT-X-ENDLIST\n'
rw = hls_proxy.rewrite_playlist(pl, "https://cdn.example/p/index.m3u8", cid, "https://ak/api/terabox/hls")
lines = [l for l in rw.splitlines() if "hls?u=" in l]
assert len(lines) == 3 and "cdn.example" not in rw and rw.count("#EXTINF") == 2
assert hls_proxy.read_token(lines[-1].split("u=")[1])[0] == "https://cdn.example/p/b.ts", "relative URLs resolved"
class Up2:
    def __init__(s, body, code=200, ct="video/mp2t"): s.body, s.status_code, s.headers, s.pos = body, code, {"Content-Type": ct}, 0
    def iter_content(s, n):  # stateful like a real socket stream
        while s.pos < len(s.body):
            chunk = s.body[s.pos:s.pos + n]; s.pos += len(chunk); yield chunk
    @property
    def content(s): return b"".join(s.iter_content(65536))
    def close(s): pass
seen = {}
def fake_get(u, headers=None, cookies=None, **kw):
    seen.update(u=u, h=headers, c=cookies)
    return Up2(b"#EXTM3U\nseg.ts\n", ct="application/vnd.apple.mpegurl") if u.endswith("m3u8") else Up2(b"\x47" * 1000, 206)
hls_proxy.requests.get = fake_get
st, h, body = hls_proxy.fetch(hls_proxy.make_token("https://cdn.example/x.m3u8", cid), "https://ak/hls")
assert st == 200 and b"hls?u=" in body and seen["c"] == {"csrfToken": "abc"} and seen["h"]["User-Agent"] == "UA-X"
st, h, body = hls_proxy.fetch(hls_proxy.make_token("https://cdn.example/s.ts", cid), "https://ak/hls", "bytes=0-99")
assert st == 206 and len(b"".join(body)) == 1000 and seen["h"]["Range"] == "bytes=0-99"
try: hls_proxy.fetch(hls_proxy.make_token("https://cdn.example/s.ts", "nope"), "p"); raise SystemExit("x")
except LookupError: pass
print("OK 11: HLS proxy (sign, rewrite, cookie replay, range, expiry)")

# --- 12) resolver keeps the playlist session for the proxy
config.FLARESOLVERR_URL = "http://flare.test/v1"; config.TERABOX_COOKIE = ""; config.TERABOX_THIRD_PARTY_URL = "off"
fake, calls = make_fs([("shorturlinfo", {"errno": 0, "shareid": 1, "uk": 2, "sign": "s", "timestamp": 3}),
    ("/share/streaming", "#EXTM3U\n"), ("/share/list", {"errno": 0, "list": [{"server_filename": "v.mp4", "size": "9", "fs_id": 7, "isdir": "0", "category": 1}]}), ("/s/1", "<html/>")])
def fs_stream(url, session=None, cookies=None):
    if "/share/streaming" in url: return {"url": url, "response": "<pre>#EXTM3U\nx.ts</pre>", "cookies": [{"name": "csrfToken", "value": "z"}], "userAgent": "FS-UA"}
    return fake(url, session, cookies)
tr._session = lambda: Blocked(); fs.get = fs_stream; fs.post = fake.post; reset()
out = tr.resolve_terabox("https://terabox.com/s/1vid")
sc = out["_stream_ctx"]["v.mp4"]
assert sc["cookies"] == {"csrfToken": "z"} and sc["ua"] == "FS-UA" and "fid=7" in sc["url"]
print("OK 12: stream session captured for proxy")
