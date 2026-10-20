#!/usr/bin/env python3
"""TV Box server: serves the TV launcher (on this Mac) and the phone remote (on your Wi-Fi).

    python3 server.py [--port 8765] [--open]

--open launches the launcher in Chrome once the server is up.
"""

import argparse
import json
import secrets
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import macinput

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
STATE_DIR = Path.home() / ".tvbox"
TOKEN_FILE = STATE_DIR / "token"
TV_WINDOW_FILE = STATE_DIR / "chrome_window"

# Browser shortcuts the remote's buttons map to (Chrome on macOS).
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


def chrome_open(url):
    """Show url in TV Box's own Chrome window, full-screen, leaving other windows alone.

    The window is remembered by Chrome's window id; if it was closed (or Chrome restarted)
    a fresh one is made. Launches Chrome if it isn't running.
    """
    url = url.replace('"', "%22")
    try:
        tv_id = int(TV_WINDOW_FILE.read_text())
    except (OSError, ValueError):
        tv_id = 0
    new_id = osascript(f'''
        tell application "Google Chrome"
            try
                set tvWindow to first window whose id is {tv_id}
            on error
                set tvWindow to make new window
            end try
            set URL of active tab of tvWindow to "{url}"
            set index of tvWindow to 1
            activate
            return id of tvWindow
        end tell''', timeout=20)
    if new_id.isdigit() and int(new_id) != tv_id:
        TV_WINDOW_FILE.write_text(new_id)
    osascript('''
        delay 0.5
        tell application "System Events" to tell process "Google Chrome"
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

    def do_POST(self):
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
                chrome_open(f"http://localhost:{self.port}/tv")
            elif cmd == "open":
                apps = json.loads((WEB / "apps.json").read_text())
                app = next(a for a in apps if a["id"] == body["id"])
                chrome_open(app["url"])
            elif cmd == "volume":
                return {"ok": True, "volume": volume(body.get("action"))}
            elif cmd == "status":
                return {"ok": True, "volume": volume(), "trusted": macinput.trusted()}
            else:
                raise ValueError(cmd)
        return {"ok": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open the launcher in Chrome")
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
        threading.Timer(1.0, chrome_open, [f"http://localhost:{args.port}/tv"]).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
