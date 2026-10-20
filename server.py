#!/usr/bin/env python3
"""TV Box server: serves the TV launcher (on this Mac) and the phone remote (on your Wi-Fi).

    python3 server.py [--port 8765] [--open]

--open shows the launcher in Safari once the server is up.
"""

import argparse
import hashlib
import io
import json
import secrets
import socket
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import macinput

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
STATE_DIR = Path.home() / ".tvbox"
TOKEN_FILE = STATE_DIR / "token"
WALLPAPER_DIR = Path.home() / "Pictures" / "TV Wallpapers"
WALLPAPER_CACHE = STATE_DIR / "wallpaper-cache"
WALLPAPER_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".tif", ".tiff"}
# Apple TV aerial screensaver catalog, streamed from Apple's CDN. Chrome doesn't trust the CA
# behind sylvan.apple.com, so videos are relayed through this server (see proxy_aerial).
AERIALS_CATALOG_URL = "https://sylvan.apple.com/Aerials/resources-16.tar"
AERIALS_FILE = STATE_DIR / "aerials.json"
AERIAL_CATEGORIES = {"AerialCategoryLandscapes"}  # also: Cities, Underwater, Space
BING_URL = "https://www.bing.com/HPImageArchive.aspx?format=js&idx={idx}&n=8&mkt=en-IN"

# Browser shortcuts the remote's buttons map to (Safari on macOS).
SHORTCUTS = {
    "back": ("[", ["cmd"]),
    "forward": ("]", ["cmd"]),
    "fullscreen": ("f", ["cmd", "ctrl"]),
    "playpause": ("space", []),
}


def load_token():
    STATE_DIR.mkdir(exist_ok=True)
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(12))
        TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE.read_text().strip()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def osascript(script, timeout=5):
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.strip()


def tv_open(url):
    """Show url on the TV: Safari's front window, full-screen. Launches Safari if needed.

    The TV uses Safari so it never mixes with your everyday Chrome windows (and Netflix
    streams at higher resolution in Safari on a Mac).
    """
    url = url.replace('"', "%22")
    osascript(f'''
        tell application "Safari"
            activate
            if (count of windows) = 0 then
                make new document with properties {{URL:"{url}"}}
            else
                set URL of current tab of front window to "{url}"
            end if
        end tell''', timeout=20)
    osascript('''
        delay 0.5
        tell application "System Events" to tell process "Safari"
            try
                if value of attribute "AXFullScreen" of front window is false then ¬
                    set value of attribute "AXFullScreen" of front window to true
            end try
        end tell''', timeout=20)


def volume(action=None):
    if action == "up":
        osascript("set volume output volume ((output volume of (get volume settings)) + 6)")
    elif action == "down":
        osascript("set volume output volume ((output volume of (get volume settings)) - 6)")
    elif action == "mute":
        osascript("set volume output muted (not (output muted of (get volume settings)))")
    settings = osascript("get volume settings")  # "output volume:50, input volume:..., output muted:false"
    parts = dict(p.strip().split(":", 1) for p in settings.split(",") if ":" in p)
    level = parts.get("output volume", "missing value")
    return {
        "level": None if level == "missing value" else int(level),  # None = output (e.g. HDMI) has no software volume
        "muted": parts.get("output muted") == "true",
    }


def wallpapers():
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in WALLPAPER_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in WALLPAPER_EXT and not p.name.startswith("."))


def refresh_aerials_catalog():
    """Fetch Apple's aerial catalog (entries.json inside a tar) at most once a week."""
    try:
        if time.time() - AERIALS_FILE.stat().st_mtime < 7 * 86400:
            return
    except OSError:
        pass
    try:
        with urllib.request.urlopen(AERIALS_CATALOG_URL, timeout=30) as r:
            with tarfile.open(fileobj=io.BytesIO(r.read())) as tar:
                entries = tar.extractfile("entries.json").read()
        json.loads(entries)  # only keep it if it parses
        AERIALS_FILE.write_bytes(entries)
    except (OSError, ValueError, KeyError, tarfile.TarError):
        pass  # keep whatever copy we had


def aerial_videos():
    """Landscape aerials: [{title, video (4K HEVC), fallback (1080p H.264)}], as local relay URLs."""
    refresh_aerials_catalog()
    try:
        data = json.loads(AERIALS_FILE.read_text())
    except (OSError, ValueError):
        return []
    wanted = {c["id"] for c in data.get("categories", []) if c.get("localizedNameKey") in AERIAL_CATEGORIES}
    relay = lambda url: "/aerial?u=" + quote(url, safe="") if url else None
    return [{"title": a.get("accessibilityLabel", ""),
             "video": relay(a.get("url-4K-SDR") or a["url-1080-H264"]),
             "fallback": relay(a["url-1080-H264"])}
            for a in data.get("assets", [])
            if a.get("url-1080-H264") and wanted & set(a.get("categories", []))
            and a.get("includeInShuffle", True) is not False]


_bing = {"at": 0.0, "photos": []}


def scenic_photos():
    """Bing's recent photos of the day (mostly landscapes), cached for six hours: [{title, img}]."""
    if time.time() - _bing["at"] < 6 * 3600:
        return _bing["photos"]
    photos = []
    for idx in (0, 8):
        try:
            with urllib.request.urlopen(BING_URL.format(idx=idx), timeout=8) as r:
                for im in json.load(r).get("images", []):
                    photos.append({"title": im.get("copyright", "").split(" (©")[0],
                                   "img": f"https://www.bing.com{im['urlbase']}_UHD.jpg"})
        except (OSError, ValueError, KeyError):
            pass
    _bing.update(at=time.time() if photos else 0.0, photos=photos)
    return photos


def wallpaper_jpeg(name):
    """A screen-sized JPEG of a wallpaper photo, converted once with sips (handles iPhone HEIC)."""
    src = WALLPAPER_DIR / name
    if src.parent != WALLPAPER_DIR or not src.is_file() or src.suffix.lower() not in WALLPAPER_EXT:
        return None
    st = src.stat()
    key = hashlib.sha1(f"{name}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:20]
    out = WALLPAPER_CACHE / f"{key}.jpg"
    if not out.exists():
        WALLPAPER_CACHE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85", "-Z", "3840",
                        str(src), "--out", str(out)], capture_output=True, timeout=60)
    return out.read_bytes() if out.exists() else None


def screen_locked():
    """True while the macOS lock screen is up (the remote can't get past it, by design)."""
    out = subprocess.run(["ioreg", "-n", "Root", "-d1"], capture_output=True, text=True).stdout
    return '"CGSSessionScreenIsLocked"=Yes' in out.replace(" ", "")


def lock_screen():
    macinput.key("q", ["ctrl", "cmd"])  # the system Lock Screen shortcut
    time.sleep(1.5)
    subprocess.run(["pmset", "displaysleepnow"])


class Waker:
    """Wakes the display when the remote is used after a quiet spell."""

    def __init__(self):
        self.last = 0.0

    def poke(self):
        now = time.time()
        if now - self.last > 20:
            subprocess.Popen(["caffeinate", "-u", "-t", "2"])
        self.last = now


class Handler(BaseHTTPRequestHandler):
    token = ""
    port = 8765
    waker = Waker()
    input_lock = threading.Lock()

    def log_message(self, fmt, *args):
        # Only log the phone side; the launcher's own requests are noise.
        if not self.is_local():
            print(f"{self.client_address[0]} {self.command} {urlparse(self.path).path}", flush=True)

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def file(self, name, ctype):
        self.send(200, (WEB / name).read_bytes(), ctype)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/tv" if self.is_local() else "/remote")
            self.end_headers()
        elif path == "/tv":
            self.file("tv.html", "text/html; charset=utf-8")
        elif path == "/remote":
            # Point the manifest at the paired start URL in the HTML itself: iOS reads it at
            # load time, and a home-screen app has its own storage, so it must start with the token.
            html = (WEB / "remote.html").read_text()
            t = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            if t and secrets.compare_digest(t, self.token):
                html = html.replace('href="/manifest.webmanifest"', f'href="/manifest.webmanifest?t={t}"')
            self.send(200, html.encode(), "text/html; charset=utf-8")
        elif path == "/apps.json":
            self.file("apps.json", "application/json")
        elif path == "/manifest.webmanifest":
            # Bake the token into start_url so the installed app opens already paired.
            t = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            start = f"/remote?t={t}" if secrets.compare_digest(t, self.token) else "/remote"
            self.send(200, {
                "name": "TV Remote",
                "short_name": "TV Remote",
                "start_url": start,
                "scope": "/",
                "display": "standalone",
                "orientation": "portrait",
                "background_color": "#0b0d12",
                "theme_color": "#0b0d12",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                ],
            }, "application/manifest+json")
        elif path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            self.file(path[1:], "image/png")
        elif path == "/sw.js":
            self.file("sw.js", "text/javascript")
        elif path == "/aerial":
            if not self.is_local():
                return self.send(403, {"error": "local only"})
            self.proxy_aerial(parse_qs(urlparse(self.path).query).get("u", [""])[0])
        elif path == "/api/wallpapers" or path.startswith("/wallpapers/"):
            # Personal photos: only the TV screen on this Mac gets them.
            if not self.is_local():
                return self.send(403, {"error": "local only"})
            if path == "/api/wallpapers":
                return self.send(200, {
                    "videos": aerial_videos(),
                    "photos": [{"title": "", "img": "/wallpapers/" + quote(n)} for n in wallpapers()],
                    "scenic": scenic_photos(),
                })
            data = wallpaper_jpeg(unquote(path[len("/wallpapers/"):]))
            if data is None:
                return self.send(404, {"error": "not found"})
            self.send(200, data, "image/jpeg")
        elif path == "/api/pair":
            # Only the launcher running on this Mac may read the pairing link.
            if not self.is_local():
                return self.send(403, {"error": "local only"})
            self.send(200, {
                "url": f"http://{lan_ip()}:{self.port}/remote?t={self.token}",
                "trusted": macinput.trusted(),
            })
        else:
            self.send(404, {"error": "not found"})

    def proxy_aerial(self, url):
        """Relay an aerial video from Apple's CDN, passing Range through so the browser can seek."""
        if not url.startswith("https://sylvan.apple.com/"):
            return self.send(403, {"error": "not an aerial"})
        headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        try:
            upstream = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20)
        except urllib.error.HTTPError as e:
            upstream = e
        except OSError:
            return self.send(502, {"error": "aerial unavailable"})
        with upstream:
            self.send_response(upstream.status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if upstream.headers.get(h):
                    self.send_header(h, upstream.headers[h])
            self.end_headers()
            try:
                while chunk := upstream.read(256 * 1024):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass  # the browser often drops a range request once it has what it needs

    def do_POST(self):
        if urlparse(self.path).path == "/api/wallstatus" and self.is_local():
            # The TV page reports what wallpaper mode is showing, for the log.
            body = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 500))
            print("  wallpaper:", body.decode(errors="replace"), flush=True)
            return self.send(200, {"ok": True})
        if urlparse(self.path).path != "/api/cmd":
            return self.send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if not secrets.compare_digest(str(body.get("t", "")), self.token):
            return self.send(401, {"error": "bad token, rescan the QR code on the TV"})
        try:
            self.send(200, self.run(body))
        except (KeyError, ValueError, TypeError, StopIteration) as e:
            self.send(400, {"error": f"bad command: {e}"})

    def run(self, body):
        cmd = body.get("cmd")
        if cmd not in ("move", "scroll"):
            print(f"  cmd={cmd} {body.get('name') or body.get('button') or body.get('action') or ''}".rstrip(), flush=True)
        if cmd not in ("status", "volume"):
            self.waker.poke()
        if cmd == "status":
            return {"ok": True, "volume": volume(), "trusted": macinput.trusted(), "locked": screen_locked()}
        if cmd == "lock":
            lock_screen()
            return {"ok": True, "locked": True}
        # Anything that drives the screen is pointless (and blocked by macOS) at the lock screen.
        if cmd not in ("move", "scroll", "volume") and screen_locked():
            return {"ok": False, "locked": True}
        with self.input_lock:
            if cmd == "key":
                name = body["name"]
                if name in SHORTCUTS:
                    macinput.key(*SHORTCUTS[name])
                else:
                    macinput.key(name, [m for m in body.get("mods", []) if m in macinput.MODS])
            elif cmd == "move":
                macinput.move(float(body["dx"]), float(body["dy"]))
            elif cmd == "click":
                macinput.click(right=body.get("button") == "right")
            elif cmd == "scroll":
                macinput.scroll(float(body["dy"]))
            elif cmd == "type":
                macinput.type_text(str(body["text"])[:500])
            elif cmd == "home":
                tv_open(f"http://localhost:{self.port}/tv")
            elif cmd == "wallpaper":
                tv_open(f"http://localhost:{self.port}/tv#wallpaper")
            elif cmd == "open":
                apps = json.loads((WEB / "apps.json").read_text())
                app = next(a for a in apps if a["id"] == body["id"])
                tv_open(app["url"])
            elif cmd == "volume":
                return {"ok": True, "volume": volume(body.get("action"))}
            else:
                raise ValueError(cmd)
        return {"ok": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open the launcher in Safari")
    args = ap.parse_args()

    Handler.token = load_token()
    Handler.port = args.port
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"TV launcher : http://localhost:{args.port}/tv")
    print(f"Phone remote: http://{lan_ip()}:{args.port}/remote?t={Handler.token}")
    if not macinput.trusted():
        print("! Accessibility permission missing: the remote can't press keys or move the mouse yet.")
        print("  System Settings > Privacy & Security > Accessibility > enable the app running this server.")
    if args.open:
        threading.Timer(1.0, tv_open, [f"http://localhost:{args.port}/tv"]).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
