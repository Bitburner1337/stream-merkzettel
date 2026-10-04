"""
Stream-Merkzettel – Desktop-Version
Clip-Stellen und verpasste Abschnitte in Streams notieren.

Globale Hotkeys (funktionieren auch im Vollbild):
  F9  = Clip hier
  F10 = Bin weg / Wieder da
  F8  = Stoppuhr Start/Pause
(änderbar unter "Einstellungen")

Twitch: Anmeldung per Gerätecode (Device Code Flow), keine eigene Server-Komponente nötig.
Für eigene Builds: unten bei TWITCH_CLIENT_ID die Client-ID einer Twitch-Anwendung
vom Typ "Öffentlich" eintragen (dev.twitch.tv/console).
"""

import json
import os
import base64
import queue
import random
import re
import socket
import ssl
import subprocess
import struct
import sys
import threading
import time
import uuid
import webbrowser
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

try:
    import sv_ttk
except ImportError:
    sv_ttk = None

try:
    from pynput import keyboard as pkb
except ImportError:
    pkb = None

try:
    import winsound
except ImportError:
    winsound = None


# ---------------------------------------------------------------- Speicherort
def data_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "StreamMerkzettel")
    os.makedirs(d, exist_ok=True)
    return d


DATA_FILE = os.path.join(data_dir(), "daten.json")

DEFAULT_SETTINGS = {
    "hk_clip": "<f9>",
    "hk_gap": "<f10>",
    "hk_clock": "<f8>",
    "sound": True,
    "theme": "dark",
    "notify": "all",      # all | favs | off
    "favs": [],
    "hidden": [],         # [{"id","name"}]  – ausgeblendete Kanäle
    "extra": [],          # [{"id","login","name"}] – zusätzliche Kanäle ohne Folgen
    "chat": True,
    "emotes": True,
    "shortcut_asked": False,
    "streamdeck": True,
}

# ---------------------------------------------------------------- Farben
THEMES = {
    "dark": {
        "bg": "#1c1c1c", "fg": "#fafafa", "muted": "#9a9a9a", "field": "#2b2b2b",
        "track": "#2a2a2a", "clip": "#F2B744", "clip_hover": "#F7C966", "clip_fg": "#1c1c1c",
        "gap": "#4F8FD9", "gap_hover": "#68A2E6", "gap_fg": "#ffffff", "stripe": "#3A6CA8",
        "done": "#555555", "now": "#fafafa", "danger": "#FF7A66", "menu_bg": "#2b2b2b", "live": "#FF4F5E",
    },
    "light": {
        "bg": "#fafafa", "fg": "#1c1c1c", "muted": "#6b6b6b", "field": "#ffffff",
        "track": "#e9e9e9", "clip": "#E2A11B", "clip_hover": "#ECB13A", "clip_fg": "#1c1c1c",
        "gap": "#2F6FB0", "gap_hover": "#3D80C4", "gap_fg": "#ffffff", "stripe": "#7FA7D1",
        "done": "#b8b8b8", "now": "#1c1c1c", "danger": "#C0392B", "menu_bg": "#ffffff", "live": "#E5293B",
    },
}

FONT = "Segoe UI"

# Stream-Deck-Anbindung (Plugin "Clipp Helper") – nur vom eigenen PC erreichbar
SD_PORT = 8765
SD_PLUGIN_FILE = "com.xjanx.clipp-helper.streamDeckPlugin"


# ---------------------------------------------------------------- Zeit
def parse_time(s):
    if s is None:
        return None
    s = re.sub(r"\s+", "", str(s).strip().lower())
    if not s:
        return None
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?:in)?)?(?:(\d+)s)?", s)
    if m and any(m.groups()):
        h, mi, se = (int(x) if x else 0 for x in m.groups())
        return h * 3600 + mi * 60 + se
    if re.fullmatch(r"\d+([.,]\d+)?", s):
        return round(float(s.replace(",", ".")) * 60)
    parts = re.split(r"[:.]", s)
    if 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts):
        n = [int(p) for p in parts]
        return n[0] * 3600 + n[1] * 60 + n[2] if len(n) == 3 else n[0] * 60 + n[1]
    return None


def fmt(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def vod_link(url, sec):
    if not url:
        return None
    try:
        u = urlparse(url)
        if not u.scheme:
            return None
        sec = max(0, int(sec))
        q = dict(parse_qsl(u.query))
        if "twitch.tv" in u.netloc:
            q["t"] = f"{sec // 3600}h{sec % 3600 // 60}m{sec % 60}s"
        else:
            q["t"] = f"{sec}s"
        return urlunparse(u._replace(query=urlencode(q)))
    except Exception:
        return None


def uid():
    return uuid.uuid4().hex[:10]


def now_ms():
    return time.time() * 1000


def dark_titlebar(win, dark):
    """Windows 10/11: Titelleiste passend zum Theme einfärben."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


# ---------------------------------------------------------------- Twitch
# Client-ID deiner Twitch-Anwendung (Client-Typ "Öffentlich"). Die ID ist nicht geheim.
TWITCH_CLIENT_ID = "8ji488n8j3j0e2t4fngvgfpjqz4rve"
TWITCH_SCOPES = "user:read:follows user:write:chat"
TOKEN_FILE = os.path.join(data_dir(), "twitch.json")
POLL_SECONDS = 60


class AuthLost(Exception):
    pass


def http_json(method, url, data=None, headers=None, timeout=15, json_body=None):
    body = urlencode(data).encode() if data is not None else None
    h = {"Accept": "application/json", "User-Agent": "StreamMerkzettel"}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        h["Content-Type"] = "application/json"
    elif body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
            return e.code, (json.loads(raw) if raw else {})
        except Exception:
            return e.code, {}


def iso_ms(txt):
    return datetime.strptime(txt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000


class TwitchClient:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                self.tok = json.load(f)
        except Exception:
            self.tok = {}

    @property
    def configured(self):
        return bool(TWITCH_CLIENT_ID)

    @property
    def logged_in(self):
        return bool(self.tok.get("access_token") and self.tok.get("user_id"))

    @property
    def can_write(self):
        return self.logged_in and "user:write:chat" in (self.tok.get("scopes") or [])

    @property
    def name(self):
        return self.tok.get("display_name") or self.tok.get("login") or ""

    def _save(self):
        try:
            if self.tok:
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.tok, f)
            elif os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
        except Exception:
            pass

    def _set_tokens(self, d):
        with self.lock:
            self.tok["access_token"] = d["access_token"]
            if d.get("refresh_token"):
                self.tok["refresh_token"] = d["refresh_token"]
            self._save()

    # Gerätecode-Anmeldung
    def start_device(self):
        st, d = http_json("POST", "https://id.twitch.tv/oauth2/device",
                          {"client_id": TWITCH_CLIENT_ID, "scopes": TWITCH_SCOPES})
        if st != 200:
            raise RuntimeError(d.get("message") or f"Twitch antwortet mit Fehler {st}")
        return d

    def poll_device(self, device_code):
        """True = angemeldet, False = weiter warten, Exception = abgebrochen/abgelaufen."""
        st, d = http_json("POST", "https://id.twitch.tv/oauth2/token", {
            "client_id": TWITCH_CLIENT_ID, "scopes": TWITCH_SCOPES, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if st == 200 and d.get("access_token"):
            self._set_tokens(d)
            self._fetch_user()
            return True
        msg = str(d.get("message", "")).lower()
        if "pending" in msg or "slow" in msg:
            return False
        raise RuntimeError(d.get("message") or f"Twitch antwortet mit Fehler {st}")

    def _fetch_user(self):
        st, d = http_json("GET", "https://id.twitch.tv/oauth2/validate",
                          headers={"Authorization": "OAuth " + self.tok["access_token"]})
        if st != 200:
            raise RuntimeError("Anmeldung konnte nicht geprüft werden")
        self.tok["user_id"] = d.get("user_id")
        self.tok["login"] = d.get("login")
        self.tok["scopes"] = d.get("scopes") or []
        try:
            u = self.api("/users", {"id": d.get("user_id")}).get("data") or []
            if u:
                self.tok["display_name"] = u[0].get("display_name")
        except Exception:
            pass
        with self.lock:
            self._save()

    def refresh(self):
        rt = self.tok.get("refresh_token")
        if not rt:
            return False
        st, d = http_json("POST", "https://id.twitch.tv/oauth2/token",
                          {"client_id": TWITCH_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt})
        if st == 200 and d.get("access_token"):
            self._set_tokens(d)
            return True
        return False

    def api(self, path, params=None, _retry=True):
        url = "https://api.twitch.tv/helix" + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        st, d = http_json("GET", url, headers={"Client-Id": TWITCH_CLIENT_ID,
                                               "Authorization": "Bearer " + self.tok.get("access_token", "")})
        if st == 401 and _retry:
            if self.refresh():
                return self.api(path, params, False)
            self.logout(revoke=False)
            raise AuthLost()
        if st == 401:
            self.logout(revoke=False)
            raise AuthLost()
        if st != 200:
            raise RuntimeError(d.get("message") or f"Twitch antwortet mit Fehler {st}")
        return d

    def followed_live(self):
        out, cursor = [], None
        for _ in range(5):
            p = {"user_id": self.tok["user_id"], "first": 100}
            if cursor:
                p["after"] = cursor
            d = self.api("/streams/followed", p)
            out += d.get("data", [])
            cursor = (d.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return out

    def send_chat(self, broadcaster_id, message):
        """Gibt (ok, grund) zurück."""
        def post():
            return http_json("POST", "https://api.twitch.tv/helix/chat/messages", headers={
                "Client-Id": TWITCH_CLIENT_ID, "Authorization": "Bearer " + self.tok.get("access_token", "")},
                json_body={"broadcaster_id": broadcaster_id, "sender_id": self.tok.get("user_id"),
                           "message": message})
        st, d = post()
        if st == 401 and self.refresh():
            st, d = post()
        if st == 401:
            return False, "Keine Berechtigung – bitte einmal neu mit Twitch anmelden."
        if st != 200:
            return False, d.get("message") or f"Fehler {st}"
        r = (d.get("data") or [{}])[0]
        if r.get("is_sent"):
            return True, ""
        reason = (r.get("drop_reason") or {}).get("message") or "Nachricht wurde nicht gesendet."
        return False, reason

    def user_by_login(self, login):
        d = self.api("/users", {"login": login.strip().lower().lstrip("@")})
        return (d.get("data") or [None])[0]

    def streams_by_ids(self, ids):
        out = []
        for i in range(0, len(ids), 100):
            d = self.api("/streams", {"user_id": ids[i:i + 100], "first": 100})
            out += d.get("data", [])
        return out

    def find_vod(self, user_id, stream_id):
        d = self.api("/videos", {"user_id": user_id, "type": "archive", "first": 10})
        for v in d.get("data", []):
            if str(v.get("stream_id")) == str(stream_id):
                return v.get("url")
        return None

    def logout(self, revoke=True):
        at = self.tok.get("access_token")
        with self.lock:
            self.tok = {}
            self._save()
        if revoke and at:
            try:
                http_json("POST", "https://id.twitch.tv/oauth2/revoke",
                          {"client_id": TWITCH_CLIENT_ID, "token": at})
            except Exception:
                pass


# ---------------------------------------------------------------- Twitch-Chat (nur lesen)
CHAT_COLORS = ["#FF6B6B", "#4FC3F7", "#81C784", "#FFB74D", "#BA68C8", "#4DB6AC", "#F06292",
               "#AED581", "#7986CB", "#FFD54F", "#64B5F6", "#E57373"]


class ChatClient:
    """Liest den Chat eines Kanals anonym über Twitchs Chat-Server mit (WebSocket, Port 443)."""

    HOST = "irc-ws.chat.twitch.tv"

    def __init__(self, out_queue):
        self.q = out_queue
        self.want = None
        self.thread = None

    def set_channel(self, login):
        self.want = login.lower() if login else None
        if self.want and not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    # --- minimaler WebSocket-Client (ohne Zusatzpakete)
    def _connect(self):
        raw = socket.create_connection((self.HOST, 443), timeout=10)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.HOST)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET / HTTP/1.1\r\nHost: {self.HOST}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError("Handshake fehlgeschlagen")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("Chat-Server lehnt ab")
        return sock, rest

    @staticmethod
    def _frame(data, opcode=1):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
        return hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))

    @staticmethod
    def _parse(buf):
        frames = []
        while len(buf) >= 2:
            op, n, i = buf[0] & 0x0F, buf[1] & 0x7F, 2
            if n == 126:
                if len(buf) < 4:
                    break
                n, i = struct.unpack(">H", buf[2:4])[0], 4
            elif n == 127:
                if len(buf) < 10:
                    break
                n, i = struct.unpack(">Q", buf[2:10])[0], 10
            mask = None
            if buf[1] & 0x80:
                mask, i = buf[i:i + 4], i + 4
            if len(buf) < i + n:
                break
            payload = buf[i:i + n]
            if mask:
                payload = bytes(b ^ mask[k % 4] for k, b in enumerate(payload))
            frames.append((op, payload))
            buf = buf[i + n:]
        return frames, buf

    def run(self):
        while True:
            want = self.want
            if not want:
                time.sleep(0.3)
                continue
            sock = None
            try:
                sock, buf = self._connect()
                sock.settimeout(1.0)

                def send(line, opcode=1):
                    sock.sendall(self._frame(line.encode("utf-8") if isinstance(line, str) else line, opcode))

                send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                send("PASS SCHMOOPIIE")
                send(f"NICK justinfan{random.randint(10000, 99999)}")
                send(f"JOIN #{want}")
                self.q.put(("chat_sys", want, f"Verbunden mit #{want}"))
                last, partial = time.time(), b""
                while self.want == want:
                    try:
                        data = sock.recv(16384)
                    except socket.timeout:
                        if time.time() - last > 400:
                            raise ConnectionError("Zeitüberschreitung")
                        continue
                    if not data:
                        raise ConnectionError("getrennt")
                    last = time.time()
                    frames, buf = self._parse(buf + data)
                    for op, payload in frames:
                        if op in (1, 0):
                            partial += payload
                            for line in partial.decode("utf-8", "replace").split("\r\n"):
                                if line:
                                    self.handle(line, want, send)
                            partial = b""
                        elif op == 9:
                            send(payload, 10)
                        elif op == 8:
                            raise ConnectionError("Server hat getrennt")
            except Exception:
                if self.want == want:
                    self.q.put(("chat_sys", want, "Verbindung weg – neuer Versuch in 5 s …"))
                    time.sleep(5)
            finally:
                try:
                    sock and sock.close()
                except Exception:
                    pass

    def handle(self, line, chan, send):
        tags = {}
        if line.startswith("@"):
            tagstr, _, line = line[1:].partition(" ")
            for kv in tagstr.split(";"):
                k, _, v = kv.partition("=")
                tags[k] = v
        if line.startswith("PING"):
            send("PONG" + line[4:])
            return
        if " ROOMSTATE " in line and tags.get("room-id"):
            self.q.put(("chat_room", chan, tags["room-id"]))
            return
        if " PRIVMSG " not in line:
            return
        prefix, _, rest = line.partition(" PRIVMSG ")
        _, _, msg = rest.partition(" :")
        nick = prefix.lstrip(":").split("!")[0]
        name = tags.get("display-name") or nick
        color = tags.get("color") or CHAT_COLORS[sum(map(ord, nick)) % len(CHAT_COLORS)]
        if msg.startswith("\x01ACTION ") and msg.endswith("\x01"):
            msg = msg[8:-1]
        self.q.put(("chat_msg", chan, name, color, msg, tags.get("emotes", "")))


# ---------------------------------------------------------------- Emotes (Twitch, BTTV, FFZ, 7TV)
def get_json(url):
    try:
        st, d = http_json("GET", url, timeout=15)
        return d if st == 200 else None
    except Exception:
        return None


def third_party_emotes(room_id=None):
    """Name -> (Schlüssel, Bild-URL). Ohne room_id: globale Emotes."""
    m = {}
    if room_id:
        d = get_json(f"https://api.betterttv.net/3/cached/users/twitch/{room_id}") or {}
        bttv = d.get("channelEmotes", []) + d.get("sharedEmotes", [])
        f = get_json(f"https://api.frankerfacez.com/v1/room/id/{room_id}") or {}
        t = get_json(f"https://7tv.io/v3/users/twitch/{room_id}") or {}
        seven = (t.get("emote_set") or {}).get("emotes") or []
    else:
        bttv = get_json("https://api.betterttv.net/3/cached/emotes/global") or []
        f = get_json("https://api.frankerfacez.com/v1/set/global") or {}
        seven = (get_json("https://7tv.io/v3/emote-sets/global") or {}).get("emotes") or []
    for e in bttv if isinstance(bttv, list) else []:
        m[e["code"]] = ("bttv:" + e["id"], f"https://cdn.betterttv.net/emote/{e['id']}/1x.png")
    for st in (f.get("sets") or {}).values():
        for e in st.get("emoticons", []):
            url = (e.get("urls") or {}).get("1")
            if url:
                m[e["name"]] = ("ffz:" + str(e["id"]), url if url.startswith("http") else "https:" + url)
    for e in seven:
        m[e["name"]] = ("7tv:" + e["id"], f"https://cdn.7tv.app/emote/{e['id']}/1x_static.png")
    return m


def download_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "StreamMerkzettel"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


# ---------------------------------------------------------------- Desktop-Verknüpfung
ICON_PNG = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAALLElEQVR42u2bW2ydV5XHf2t/3zk+d8eXxHHTpIlTKCCgaSOQeECGBwbBAy9giYe+AkJCM5p56EyRBgRCIHGTYBAjKNBGQiC10OkwKpSKm1tBW1G3pUmmSZsGX5PIl+PLudg+Z++9ePiO7WP7uD7HdYLjmS19fvjW8bf2+u+11v7vtfcWWmsCA6a/f1KKxaKwh1omk9HBwUMKD3tAd/v7AQwE3DRtoOn+ShNyATzAsWPv6JCYuVMI3uzV94jqngBFRLwikwqvJkLzl1deGZquiUzNG3QnAASAAzh64u73GvGfBPmAiPSISJP43aimtb+Kej8N5reK3j96+YXfrnnEw64FAKJ/6Ot75yFH+A0R7hERVD0a/fG6x5xeIlcwiBgjBlVF1f+8KtV/vvLa+bGtQJCtjL/1xKl3hUYeEjHHvXceVUXE7KFh39odVD2CGBMaVX/NuurHx4fPDTYCYaMxBvDHTr7jtGj4OxHJee+siITchE1VrTEmBJassx8cHz775EYQZIPx9L75dGfcuudFzFH1znKTGl8HgxMxgapOW+Pumrh0dqI+sZs61xfAx6r228aER/2+MB5AAu+9NSboDpz5XpQxP78xBCK3uO3kXe8R5E+qzoEE7Kem6kwQBmrtB4eH//LEis1m/W/8Z0Qkmk/2W5OID3jhnxoRHe3rO93usJdFTKeq6k2Q7XdCFgS0rDa8fXR06CpgDAwYACf2bpGgU1X9PjS+NtjqjAlTJubeHb3qN6a/fzIyVs0dIhLNoa/TjIGgxUd2CU4jres2Uh/iKAiqvBXg9OmihCurOkF7QGQ7358vaY11Nel3HuIxSMQFfQOZRQSKS4rzzQOqGgGQSdbrVkTpWVlBhnWkYcvvisBSRXnL0ZB7P5pmqbJ9JwRwHtIJeHnM8fVHSsRCWgZBAKeQiglf+kSW9rRQtdvrV408YLmqfPGnReZKSmBWqeLqf4fNIpmICxfGLOVl5aMfSGEXPGGwbcYBAz98Yo6qU+KhtDy9KBAGMFPwnBup8pVP53DFNWO2atZDmBW+/ECRa7OebEpwDZZDLRGdwAj/cn+B23tD7rg1YKEc5YSGHXDQlRM+d6bIr55b5tABg/M7c3/vIZsUvvvYIm87FnLP+5NML+iWA2AddGSFR3+zxNcfKdGeFvwWuk3zPALaYrBYUf71wQLOQyxsnBQV6M4Jvx5a5nuPlznYvnPj6z2hPSV88aclLo5bcilBaJxwM0lhfMrx7z8ukGqT101XppVOOA+5lPDMhSpf/VmJbHqzW3mFRAwm8p77zhSJ7RKfVI0Anyt67n2ggFfFmMaVjjCA+84UuJL3JGJRn3YFgBX36swI//nLMo//eZmOjGA3jG48Jnz2TIGRSUcyLuxW8cB5aE8LT56r8o1HypsGwDpozwjffazML5+rNOzbGwZgpcUCuO9MkYm8J1Uz0nlozwo/eLzML55ZprOJDuxkADoywn/8T4nfvVihIys4vwbOsxeqfO3nJQ6kGye9XQHAKyTjwsik47MPFqJSlEKqTXj25SpfebhELiVvOO5fb2o0Bv7tgQJjU454GLn99ILn3h8VqNqtw2PXPMD6aCQee26ZZy5USbcJYQD3P15mvqTRnH+dOK2vgX1h3PHQU0sk40K6TXj06SVeeM2SSW6d9XcNgJXEFAasEhOvNQIS8IZYXyu67YYcELao2+xGRzayMr1Bi+mNukVa1234P97C1tNPM6UC2fCbqKCsdSOF1n1JBK0N3dayWnxtp7vFpWfYvLs1WCkL+JXAX6sq4b3D+/WJSO1ibW1gUGcj9wtCvI9+b4IwWpP6BjITQBCuq9IJkW5dp1vxzhJtWMnuAaCqxNsSpNNZpA5hI5CoQBhntSOZbAfdB6E9Fc3NiKCuSuZt/0BVQyqlWXI9J/DOUpgaJZHtJJnrppS/SnWpRK7n+DpZItdFeX6G5avnMXOjERAaLYZMApKpNd2JZJqug2napEC5vLiurzsGYMX4Qz1HMBvSuxEwFSEIi0AFFBLJDNlcjExSazxAwFsSPW8CbePAgW4y3UfJj54nlztC14l3Ui0vUAkv0nnwGOmuI+tkldIcdmqCVDzEVWcglgT1BAasEeLxRZTFiJzF2si1p2hP5rh2dYKlpe1BMNtWElVJp7MYY3C2uuqW9U/jEKh/PIVrl0lmDpDMdjB58WkqhRk6jtzOYv4KU5f+TCrXRSJzYJNs+tJzpNq7SGQ7sbaySb+uS/uKtQ4RIZ3J0Uxps6lZYAXF9Zui0iDZsVkugnpHtuc4mYNHmRk+h3eOgyfvplJeYG7iFdp7byfTfZSZkcaydEcvi/OTiAka6G/c17VkqHtgGhQhlkiTHz2Pt1UO9p1iuTTH7NgFcodPkO68henhlxrLunqZGTlHpVxAJGC3+eX1B0BBTMD8lUu4yjLdJ1swvvMWpv/6Euo9ma4jqHe7XrC+/gAIqHeYIBYltWILxg+/hLeWruNvR4zZrmC9RwGocdb23pNUygvkx15uwfgqB0+eYqmQpzA9XuMKuxsC4Y0wXkxAeX6SxXy+ZmDvqoHdfadYKs4yN36B3OG+zbLCDPNT48TaUrjr0L2mPKBSWY7ORuxoh0NAYHFukmzPbWS6jjAzch71ju6+U1RKc8xdeZXc4ZOkO2/ZLJt4ldzhPlKdvXhnt80BQnRQpFpZrnuzYwAUYwyl4gIL83m0Rj03Ppspc91DRE9TBw6R7ughP3Ieu1yi48gd2MUCs2Mvk+k4HMlGG8g6D5PKdbM0P9Xw+/UhoRrJCwuzFBbmMMZsGzJNh8BsfpKF+dlNVLi0rFSWs4jEQGB+dpqrExUW0/UVIcX84Ucw+CDeVpAgZPqPQcTbvSMfa4t4vK0iQdBYVl0EuwxM10r0kC8oxWISI0kAyuUCVyau0Z60Tae3pgEQMbjaIqYeAGvXe4F3FmurWLuhJLYwtTInRnszqkiNKNmV7P66smBdCKqp6fZta++8x1kbhatehyS4kVeLNEgLIsjqU68ptilW128hbSPT9ayuoe4Gffz7zwLNlImUncluGh6wh9v/l8R2ie7fsEJoq9F1QwAITbRvh+zeaZCmXdiAqeXXwNxgALxCW0z4wk+KfPPRyPKRSUeqrfmNiR3r9tEu8ENPLvHk2QoAk/O+pU2RdQBIdDqm9REQuDhhV+f8tpgQmuuevKODEybahR6ejFYJsUCIN7EjJazZGmYyGa3F0ZTssNvJuKzO1+q5YYcMFYiHEehrVHj7tYnCFECxWJQwumYCintF1SDRifCWQ+HvdbSyOaPXOFp0CtRfBBgayqip3bHBV93zXv1c7Uj8/jspGvHwwHm35AJ9Nno16GvGDgTj4/+bF/Q3Royi6vah/d6YQAV9auLS2fEaB/Lr3F2MfEdVBdmHJ0Wj888i3n8nejEgdUzwYQcDwfClFwe9d/9tTBigaveP8WpNEIbOud8PD5/9BXzerFya2HhhQo/ecVevsbwgcEjV74Nj8+pEjFGl4I25e+zS0GUaX5jAw4AZu/jCFe/tx4Cl2k0LezOPvES7KQr68bFLQ6/VDof7LRZDUSiM/fXsU2r9hxQmgyAMQV303BSzg6701wRhCDLn1X9k5PKLv2rm0lSt1W6Q3HbncQL5ljHBRyL66QB1ukcvVETzvASmtoXmvX9CvP3H4eGzF6E/hEG7mRZt2dbQuq3v1IeBTwHvMybI7eXh996XBH1KMd8fufz8f220pQUA1oWIB7j1TXceCb05jfIWj+6xq7NMgrkYIEOXLw+N1tkn9TG/wzYQ3GTFE7Nbl6cbfLjf9PfvTasHAQYHfSsj/jd+PiXkumks4wAAAABJRU5ErkJggg=="


def is_exe():
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("win")


def create_desktop_shortcut():
    """Legt 'Stream-Merkzettel.lnk' auf dem Desktop an (auch bei OneDrive-Desktop)."""
    exe = sys.executable
    q = lambda t: t.replace("'", "''")
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Stream-Merkzettel.lnk'));"
          f"$s.TargetPath='{q(exe)}';"
          f"$s.WorkingDirectory='{q(os.path.dirname(exe))}';"
          f"$s.IconLocation='{q(exe)},0';"
          "$s.Description='Stream-Merkzettel';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", ps], capture_output=True, text=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip().splitlines()[-1] if r.stderr else "unbekannter Fehler")


# ---------------------------------------------------------------- Flache Farb-Buttons
class FlatButton(tk.Label):
    def __init__(self, master, text, command, color_key, app, **kw):
        super().__init__(master, text=text, cursor="hand2", padx=22, pady=10,
                         font=(FONT, 12, "bold"), **kw)
        self.command, self.key, self.app = command, color_key, app
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self.recolor(True))
        self.bind("<Leave>", lambda e: self.recolor(False))
        self.recolor(False)

    def recolor(self, hover=False):
        p = self.app.pal
        self.configure(bg=p[self.key + ("_hover" if hover else "")], fg=p[self.key + "_fg"])


# ---------------------------------------------------------------- App
class App:
    def __init__(self, root):
        self.root = root
        self.state = {"streams": [], "current": None}
        self.settings = dict(DEFAULT_SETTINGS)
        self.events = queue.Queue()
        self.listener = None
        self.last_tl = 0
        self.placeholders = []
        self.dialogs = []
        self.tw = TwitchClient()
        self.live = []
        self.live_ids = None
        self.polling = False
        self.poll_job = None
        self.last_live_render = 0
        self.login_ui = None
        self.toasts = []
        self.chat = ChatClient(self.events)
        self.chat_channel = None
        self.chat_buf = []
        self.chat_tags = set()
        self.manage_ui = None
        self.sd_srv = None
        self.sd_state = ""
        self.sd_status = {}
        self.chat_room_id = None
        self.emote_imgs = {}
        self.emote_maps = {}
        self.dl_q = queue.Queue()
        for _ in range(4):
            threading.Thread(target=self._emote_worker, daemon=True).start()
        threading.Thread(target=lambda: self.events.put(("emote_map", "global", third_party_emotes())),
                         daemon=True).start()

        self.load()
        self.pal = THEMES[self.settings.get("theme", "dark")]
        self.build_ui()
        self.apply_theme()
        self.start_hotkeys()
        self.render()
        self.update_account_ui()
        self.apply_chat_visibility(initial=True)
        if self.settings.get("streamdeck", True):
            self.start_sd_server()
        self.tick()
        self.root.after(100, self.poll_events)
        self.root.after(800, self.twitch_poll)
        if is_exe() and not self.settings.get("shortcut_asked"):
            self.banner.pack(fill="x", pady=(0, 14), before=self.body_fr)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------ Daten
    def load(self):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("streams"), list):
                self.state = {"streams": d["streams"], "current": d.get("current")}
            self.settings.update(d.get("settings", {}))
        except FileNotFoundError:
            pass
        except Exception as e:
            messagebox.showwarning("Daten", f"Konnte Daten nicht lesen:\n{e}")
        if self.settings.get("theme") not in THEMES:
            self.settings["theme"] = "dark"
        if not self.cur() and self.state["streams"]:
            self.state["current"] = self.state["streams"][0]["id"]

    def save(self):
        d = dict(self.state)
        d["settings"] = self.settings
        tmp = DATA_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, DATA_FILE)
        except Exception as e:
            messagebox.showerror("Speichern", f"Speichern fehlgeschlagen:\n{e}")

    def cur(self):
        for s in self.state["streams"]:
            if s["id"] == self.state.get("current"):
                return s
        return None

    @staticmethod
    def clock_now(s):
        if not s:
            return 0
        c = s.get("clock") or {"base": 0, "startedAt": None}
        extra = (now_ms() - c["startedAt"]) / 1000 if c.get("startedAt") else 0
        return c.get("base", 0) + extra

    @staticmethod
    def running(s):
        return bool(s and s.get("clock", {}).get("startedAt"))

    # ------------------------------------------------------------ Theme
    def apply_theme(self):
        name = self.settings.get("theme", "dark")
        self.pal = p = THEMES[name]
        style = ttk.Style()
        if sv_ttk:
            sv_ttk.set_theme(name)
        else:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                            bordercolor=p["track"], lightcolor=p["bg"], darkcolor=p["bg"])
            style.configure("TButton", background=p["field"], padding=(12, 6))
            style.map("TButton", background=[("active", p["track"])])
            style.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["fg"])
            style.configure("Treeview.Heading", background=p["track"], foreground=p["fg"])
            style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"], insertcolor=p["fg"])
            style.configure("TCombobox", fieldbackground=p["field"], foreground=p["fg"])
            self.root.configure(bg=p["bg"])

        style.configure("Treeview", rowheight=32, font=(FONT, 10))
        style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
        style.configure("Title.TLabel", font=(FONT, 16, "bold"))
        style.configure("H.TLabel", font=(FONT, 12, "bold"))
        style.configure("Muted.TLabel", foreground=p["muted"], font=(FONT, 9))
        style.configure("Stats.TLabel", foreground=p["muted"], font=(FONT, 10))
        style.configure("Clock.TLabel", font=(FONT, 40, "bold"))
        style.configure("ClockOff.TLabel", font=(FONT, 40, "bold"), foreground=p["muted"])
        style.configure("Live.TLabel", foreground=p["gap"], font=(FONT, 10, "bold"))
        style.configure("Status.TLabel", foreground=p["clip"], font=(FONT, 10, "bold"))
        style.configure("Link.TLabel", foreground=p["gap"], font=(FONT, 9, "underline"))
        style.configure("Count.TLabel", foreground=p["clip"], font=(FONT, 11, "bold"))
        self.chat_txt.configure(bg=p["bg"], fg=p["fg"], insertbackground=p["fg"],
                                selectbackground=p["gap"])
        self.chat_txt.tag_configure("sys", foreground=p["muted"], font=(FONT, 9, "italic"))
        self.live_tree.tag_configure("fav", foreground=p["clip"])
        self.live_tree.tag_configure("linked", foreground=p["gap"])

        self.canvas.configure(bg=p["bg"])
        for b in (self.btn_clip, self.btn_gap):
            b.recolor(False)
        for e in self.placeholders:
            e.configure(foreground=p["muted"] if e._ph_on else p["fg"])
        for t, kind in ((self.clip_tree, "c"), (self.gap_tree, "g")):
            t.tag_configure("done", foreground=p["done"])
            t.tag_configure("open", foreground=p["fg"])
        for m in (self.more_menu,):
            self.style_menu(m)
        dark_titlebar(self.root, name == "dark")
        for d in self.dialogs:
            if d.winfo_exists():
                dark_titlebar(d, name == "dark")
        self.draw_timeline()
        self.update_clock()

    def style_menu(self, m):
        p = self.pal
        m.configure(bg=p["menu_bg"], fg=p["fg"], activebackground=p["gap"], activeforeground="#ffffff",
                    bd=0, relief="flat", font=(FONT, 10))

    def toggle_theme(self):
        self.settings["theme"] = "light" if self.settings.get("theme") == "dark" else "dark"
        self.save()
        self.apply_theme()

    # ------------------------------------------------------------ UI
    def build_ui(self):
        r = self.root
        r.title("Stream-Merkzettel")
        try:
            self._icon = tk.PhotoImage(data=ICON_PNG)
            r.iconphoto(True, self._icon)
        except Exception:
            pass
        r.geometry("1460x860")
        r.minsize(1240, 720)

        outer = ttk.Frame(r, padding=(20, 16, 20, 14))
        outer.pack(fill="both", expand=True)

        # --- Kopfzeile
        top = ttk.Frame(outer)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Stream-Merkzettel", style="Title.TLabel").grid(row=0, column=0, padx=(0, 18))
        self.stream_var = tk.StringVar()
        self.stream_cb = ttk.Combobox(top, textvariable=self.stream_var, state="readonly", width=22,
                                      font=(FONT, 10))
        self.stream_cb.grid(row=0, column=1, sticky="ew")
        self.stream_cb.bind("<<ComboboxSelected>>", self.on_select_stream)
        ttk.Button(top, text="+  Neuer Stream", style="Accent.TButton",
                   command=lambda: self.stream_dialog(False)).grid(row=0, column=2, padx=(8, 4))
        ttk.Button(top, text="Bearbeiten", command=lambda: self.stream_dialog(True)).grid(row=0, column=3)
        self.chat_btn = ttk.Button(top, text="Chat", width=10, command=self.toggle_chat)
        self.chat_btn.grid(row=0, column=4, padx=(24, 4))
        self.more_btn = ttk.Menubutton(top, text="Mehr")
        self.more_btn.grid(row=0, column=5, padx=4)
        self.more_menu = tk.Menu(self.more_btn, tearoff=0)
        self.more_menu.add_command(label="Liste kopieren", command=self.copy_text)
        self.more_menu.add_command(label="Backup kopieren", command=self.export_json)
        self.more_menu.add_command(label="Backup einfügen …", command=self.import_dialog)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Datenordner öffnen", command=self.open_folder)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Stream löschen …", command=self.delete_stream)
        self.more_btn["menu"] = self.more_menu
        self.set_btn = ttk.Button(top, text="Einstellungen", command=self.toggle_settings)
        self.set_btn.grid(row=0, column=6, padx=(4, 0))

        ttk.Separator(outer).pack(fill="x", pady=14)

        self.build_settings_page(outer)
        self.build_shortcut_banner(outer)
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        self.body_fr = body
        self.build_sidebar(body)
        ttk.Separator(body, orient="vertical").pack(side="left", fill="y", padx=22)
        self.build_chat(body)
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)
        self.main_fr = main

        # --- Steuerung
        ctl = ttk.Frame(main)
        ctl.pack(fill="x")
        self.clock_lbl = ttk.Label(ctl, text="0:00:00", style="ClockOff.TLabel", width=8)
        self.clock_lbl.pack(side="left")
        cbtn = ttk.Frame(ctl)
        cbtn.pack(side="left", padx=(6, 0))
        self.btn_start = ttk.Button(cbtn, text="▶  Start", width=12, command=self.toggle_clock)
        self.btn_start.pack(fill="x")
        ttk.Button(cbtn, text="Zeit setzen", width=12, command=self.set_time).pack(fill="x", pady=(6, 0))

        ttk.Separator(ctl, orient="vertical").pack(side="left", fill="y", padx=24)

        self.btn_clip = FlatButton(ctl, "✂  Clip hier", self.clip_now, "clip", self)
        self.btn_clip.pack(side="left")
        self.btn_gap = FlatButton(ctl, "⏸  Bin weg", self.gap_now, "gap", self)
        self.btn_gap.pack(side="left", padx=10)

        info = ttk.Frame(ctl)
        info.pack(side="left", padx=14, fill="y")
        self.live_lbl = ttk.Label(info, text="", style="Live.TLabel")
        self.live_lbl.pack(anchor="w", pady=(6, 0))
        hk_row = ttk.Frame(main)
        hk_row.pack(fill="x", pady=(8, 0))
        self.hk_lbl = ttk.Label(hk_row, text="", style="Muted.TLabel")
        self.hk_lbl.pack(side="left")
        self.status = ttk.Label(hk_row, text="", style="Status.TLabel")
        self.status.pack(side="right")

        # --- Zeitleiste
        tlh = ttk.Frame(main)
        tlh.pack(fill="x", pady=(18, 4))
        ttk.Label(tlh, text="Zeitleiste", style="H.TLabel").pack(side="left")
        self.stats_lbl = ttk.Label(tlh, text="", style="Stats.TLabel")
        self.stats_lbl.pack(side="right")
        self.canvas = tk.Canvas(main, height=78, highlightthickness=0, bd=0)
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", lambda e: self.draw_timeline())

        # --- Listen
        lists = ttk.Frame(main)
        lists.pack(fill="both", expand=True, pady=(14, 0))
        lists.columnconfigure(0, weight=1, uniform="l")
        lists.columnconfigure(1, weight=1, uniform="l")
        lists.rowconfigure(0, weight=1)

        cf = ttk.Frame(lists)
        cf.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        gf = ttk.Frame(lists)
        gf.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        self.clip_tree = self.make_list(cf, "Clippen", ("ok", "zeit", "notiz"),
                                        ("", "Zeit", "Notiz"), (40, 90, 200), "c")
        self.gap_tree = self.make_list(gf, "Nicht geschaut", ("ok", "zeit", "dauer", "notiz"),
                                       ("", "Von – bis", "Min", "Notiz"), (40, 160, 50, 140), "g")

        # Eingabe Clip
        ca = ttk.Frame(cf)
        ca.pack(fill="x", pady=(8, 0))
        self.e_clip_t = ttk.Entry(ca, width=8, font=(FONT, 10))
        self.e_clip_n = ttk.Entry(ca, font=(FONT, 10))
        self.e_clip_t.pack(side="left")
        self.e_clip_n.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(ca, text="Hinzufügen", style="Accent.TButton", command=self.add_clip).pack(side="left")
        self.placeholder(self.e_clip_t, "1:23:45")
        self.placeholder(self.e_clip_n, "Was passiert da?")
        for e in (self.e_clip_t, self.e_clip_n):
            e.bind("<Return>", lambda ev: self.add_clip())

        # Eingabe Lücke
        ga = ttk.Frame(gf)
        ga.pack(fill="x", pady=(8, 0))
        self.e_gap_a = ttk.Entry(ga, width=7, font=(FONT, 10))
        self.e_gap_b = ttk.Entry(ga, width=7, font=(FONT, 10))
        self.e_gap_n = ttk.Entry(ga, font=(FONT, 10))
        self.e_gap_a.pack(side="left")
        self.e_gap_b.pack(side="left", padx=6)
        self.e_gap_n.pack(side="left", fill="x", expand=True)
        ttk.Button(ga, text="Hinzufügen", style="Accent.TButton", command=self.add_gap).pack(side="left", padx=(6, 0))
        self.placeholder(self.e_gap_a, "von")
        self.placeholder(self.e_gap_b, "bis")
        self.placeholder(self.e_gap_n, "Notiz")
        for e in (self.e_gap_a, self.e_gap_b, self.e_gap_n):
            e.bind("<Return>", lambda ev: self.add_gap())

        ttk.Label(main, text="Zeiten: 1:23:45, 83:10, 1h23m oder 83 (= Minute 83)   ·   "
                              "Doppelklick = abhaken   ·   Entf = löschen   ·   Rechtsklick = mehr",
                  style="Muted.TLabel").pack(anchor="w", pady=(12, 0))

        if pkb is None:
            r.bind_all("<F9>", lambda e: self.clip_now())
            r.bind_all("<F10>", lambda e: self.gap_now())
            r.bind_all("<F8>", lambda e: self.toggle_clock())

    def make_list(self, parent, title, cols, heads, widths, kind):
        head = ttk.Frame(parent)
        head.pack(fill="x", pady=(0, 6))
        dot = tk.Canvas(head, width=12, height=12, highlightthickness=0, bd=0)
        dot.pack(side="left", padx=(0, 8))
        dot._kind = kind
        if kind == "c":
            self.clip_dot = dot
        else:
            self.gap_dot = dot
        ttk.Label(head, text=title, style="H.TLabel").pack(side="left")
        cnt = ttk.Label(head, text="", style="Count.TLabel")
        cnt.pack(side="left", padx=(10, 0))
        if kind == "c":
            self.clip_cnt = cnt
        else:
            self.gap_cnt = cnt

        btns = ttk.Frame(head)
        btns.pack(side="right")
        ttk.Button(btns, text="✓", width=3, command=lambda: self.toggle_done(kind)).pack(side="left", padx=1)
        ttk.Button(btns, text="Notiz", command=lambda: self.edit_note(kind)).pack(side="left", padx=1)
        ttk.Button(btns, text="VOD", command=lambda: self.open_vod(kind)).pack(side="left", padx=1)
        ttk.Button(btns, text="Löschen", command=lambda: self.delete_item(kind)).pack(side="left", padx=1)

        fr = ttk.Frame(parent)
        fr.pack(fill="both", expand=True)
        tree = ttk.Treeview(fr, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h, anchor="w")
            tree.column(c, width=w, minwidth=w if c != "notiz" else 80, stretch=(c == "notiz"),
                        anchor="center" if c == "ok" else "w")
        sb = ttk.Scrollbar(fr, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.bind("<Double-1>", lambda e: self.toggle_done(kind))
        tree.bind("<Delete>", lambda e: self.delete_item(kind))
        tree.bind("<Button-3>", lambda e: self.context_menu(e, kind))
        return tree

    def placeholder(self, entry, text):
        entry._ph = text
        entry._ph_on = True
        entry.insert(0, text)
        self.placeholders.append(entry)

        def fin(e):
            if entry._ph_on:
                entry.delete(0, "end")
                entry.configure(foreground=self.pal["fg"])
                entry._ph_on = False

        def fout(e):
            if not entry.get():
                entry.insert(0, text)
                entry.configure(foreground=self.pal["muted"])
                entry._ph_on = True

        entry.bind("<FocusIn>", fin)
        entry.bind("<FocusOut>", fout)

    def val(self, entry):
        return "" if getattr(entry, "_ph_on", False) else entry.get().strip()

    def clear(self, entry):
        entry.delete(0, "end")
        if self.root.focus_get() is not entry:
            entry.insert(0, entry._ph)
            entry.configure(foreground=self.pal["muted"])
            entry._ph_on = True

    def flash(self, msg):
        self.status.configure(text=msg)
        self.root.after(4000, lambda: self.status.configure(text="") if self.status.cget("text") == msg else None)

    def beep(self, kind):
        if not self.settings.get("sound") or winsound is None:
            return
        try:
            freq = {"clip": 1200, "gap_on": 600, "gap_off": 900, "clock": 800, "live": 1500}.get(kind, 1000)
            winsound.Beep(freq, 120)
        except Exception:
            pass

    # ------------------------------------------------------------ Rendern
    def render(self):
        self.stream_cb["values"] = [s["name"] for s in self.state["streams"]]
        s = self.cur()
        self.stream_var.set(s["name"] if s else "")
        self.render_lists()
        self.draw_timeline()
        self.update_clock()
        self.update_hk_label()
        self.update_chat_target()

    def render_lists(self):
        s = self.cur()
        for t in (self.clip_tree, self.gap_tree):
            t.delete(*t.get_children())
        if not s:
            self.stats_lbl.configure(text="Leg oben mit „Neuer Stream“ los.")
            self.clip_cnt.configure(text="")
            self.gap_cnt.configure(text="")
            return
        for c in sorted(s["clips"], key=lambda x: x["t"]):
            self.clip_tree.insert("", "end", iid=c["id"],
                                  values=("✓" if c.get("done") else "○", fmt(c["t"]), c.get("note", "")),
                                  tags=("done" if c.get("done") else "open",))
        for g in sorted(s["gaps"], key=lambda x: x["from"]):
            self.gap_tree.insert("", "end", iid=g["id"],
                                 values=("✓" if g.get("done") else "○",
                                         f"{fmt(g['from'])} – {fmt(g['to'])}",
                                         round((g["to"] - g["from"]) / 60), g.get("note", "")),
                                 tags=("done" if g.get("done") else "open",))
        total_c = len(s["clips"])
        done_c = sum(1 for c in s["clips"] if c.get("done"))
        open_c = total_c - done_c
        total_g = len(s["gaps"])
        done_g = sum(1 for g in s["gaps"] if g.get("done"))
        mins = round(sum(g["to"] - g["from"] for g in s["gaps"] if not g.get("done")) / 60)
        self.clip_cnt.configure(text=f"{done_c}/{total_c}" if total_c else "")
        self.gap_cnt.configure(text=f"{done_g}/{total_g}" if total_g else "")
        self.stats_lbl.configure(
            text=f"{done_c} von {total_c} {'Clip' if total_c == 1 else 'Clips'} geclippt   ·   "
                 f"{open_c} offen   ·   {mins} min nachzuschauen")

    def draw_timeline(self):
        p = self.pal
        for dot, key in ((getattr(self, "clip_dot", None), "clip"), (getattr(self, "gap_dot", None), "gap")):
            if dot:
                dot.configure(bg=p["bg"])
                dot.delete("all")
                dot.create_oval(1, 1, 11, 11, fill=p[key], outline="")
        if getattr(self, "live_dot", None):
            self.live_dot.configure(bg=p["bg"])
            self.live_dot.delete("all")
            self.live_dot.create_oval(1, 1, 11, 11, fill=p["live"] if self.tw.logged_in else p["done"], outline="")
        cv = self.canvas
        cv.delete("all")
        w = max(cv.winfo_width(), 10)
        th = 50  # Höhe der Leiste, darunter Skala
        cv.create_rectangle(0, 0, w, th, fill=p["track"], outline="")
        s = self.cur()
        if not s:
            return
        now = self.clock_now(s)
        max_t = max([s.get("length") or 0, now, 60]
                    + [c["t"] for c in s["clips"]] + [g["to"] for g in s["gaps"]])
        x = lambda v: v / max_t * w

        def gap_rect(a, b, done):
            x1, x2 = x(a), max(x(b), x(a) + 3)
            if done:
                cv.create_rectangle(x1, 0, x2, th, fill=p["done"], outline="")
                return
            cv.create_rectangle(x1, 0, x2, th, fill=p["gap"], outline="")
            k = x1 - th
            while k < x2:
                sx, sy = k, th
                ex, ey = k + th, 0
                if sx < x1:
                    sy -= (x1 - sx); sx = x1
                if ex > x2:
                    ey += (ex - x2); ex = x2
                if ex > sx:
                    cv.create_line(sx, sy, ex, ey, fill=p["stripe"], width=4)
                k += 12

        for g in s["gaps"]:
            gap_rect(g["from"], g["to"], g.get("done"))
        if s.get("openGap") is not None:
            gap_rect(s["openGap"], now, False)
        cv.create_rectangle(0, th, w, th + 6, fill=p["bg"], outline="")  # Streifen-Überstand abschneiden
        for c in s["clips"]:
            xx = x(c["t"])
            col = p["done"] if c.get("done") else p["clip"]
            cv.create_rectangle(xx - 1.5, 0, xx + 1.5, th, fill=col, outline="")
            cv.create_polygon(xx - 7, 0, xx + 7, 0, xx, 10, fill=col, outline="")
        if now > 0:
            cv.create_line(x(now), 0, x(now), th, fill=p["now"], width=2)
        for i in range(5):
            anchor = "nw" if i == 0 else ("ne" if i == 4 else "n")
            cv.create_text(w * i / 4, th + 8, text=fmt(max_t * i / 4), anchor=anchor,
                           fill=p["muted"], font=(FONT, 9))

    def update_clock(self):
        s = self.cur()
        run = self.running(s)
        self.clock_lbl.configure(text=fmt(self.clock_now(s)), style="Clock.TLabel" if run else "ClockOff.TLabel")
        self.btn_start.configure(text="⏸  Pause" if run else "▶  Start")
        if s and s.get("openGap") is not None:
            self.btn_gap.configure(text="▶  Wieder da")
            self.live_lbl.configure(text=f"Weg seit {fmt(s['openGap'])}")
        else:
            self.btn_gap.configure(text="⏸  Bin weg")
            self.live_lbl.configure(text="")

    def update_hk_label(self):
        if pkb is None:
            txt = "Globale Hotkeys aus (pynput fehlt) – F9/F10/F8 gehen nur, wenn das Fenster aktiv ist."
        else:
            st = self.settings
            txt = (f"Hotkeys überall, auch im Vollbild:   Clip {self.pretty(st['hk_clip'])}   ·   "
                   f"Weg / Wieder da {self.pretty(st['hk_gap'])}   ·   Uhr {self.pretty(st['hk_clock'])}")
        self.hk_lbl.configure(text=txt)

    @staticmethod
    def pretty(hk):
        return hk.replace("<", "").replace(">", "").upper()

    def tick(self):
        self.update_clock()
        s = self.cur()
        # Momentaufnahme für das Stream Deck (der Server-Thread liest nur)
        self.sd_status = {
            "stream": s["name"] if s else None,
            "time": fmt(self.clock_now(s)) if s else None,
            "seconds": int(self.clock_now(s)) if s else 0,
            "running": self.running(s),
            "away": bool(s and s.get("openGap") is not None),
            "awaySince": fmt(s["openGap"]) if s and s.get("openGap") is not None else None,
            "clips": len(s["clips"]) if s else 0,
            "clipsDone": sum(1 for c in s["clips"] if c.get("done")) if s else 0,
            "live": bool(s and s.get("tw", {}).get("autoClock")),
        }
        if self.running(s) and time.time() - self.last_tl > 5:
            self.last_tl = time.time()
            self.draw_timeline()
        if self.live and time.time() - self.last_live_render > 30:
            self.render_live()
        self.root.after(250, self.tick)

    # ------------------------------------------------------------ Stream Deck
    def start_sd_server(self):
        """Lokaler HTTP-Server für das Stream-Deck-Plugin.
        POST /clip, /gap, /clock  -> wie die Hotkeys
        GET  /status              -> aktueller Zustand als JSON"""
        if self.sd_srv:
            return
        app = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, obj=None):
                body = json.dumps(obj if obj is not None else {}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _blocked(self):
                # Browser schicken bei Anfragen von Webseiten immer einen Origin-Header mit –
                # so kann keine Webseite heimlich Clips auslösen. Das Plugin schickt keinen.
                if self.headers.get("Origin"):
                    self._send(403, {"ok": False})
                    return True
                return False

            def do_POST(self):
                if self._blocked():
                    return
                ev = self.path.strip("/")
                if ev in ("clip", "gap", "clock"):
                    if not app.sd_status.get("stream"):
                        self._send(409, {"ok": False, "error": "Kein Stream ausgewählt"})
                        return
                    app.events.put(ev)  # gleiche Warteschlange wie die Hotkeys
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"ok": False})

            def do_GET(self):
                if self._blocked():
                    return
                if self.path == "/status":
                    self._send(200, app.sd_status)
                else:
                    self._send(404, {"ok": False})

            def log_message(self, *args):
                pass  # bei --windowed gibt es keine Konsole

        try:
            srv = ThreadingHTTPServer(("127.0.0.1", SD_PORT), Handler)
        except OSError:
            self.sd_state = f"Port {SD_PORT} ist belegt – läuft das Programm schon zweimal?"
            return
        srv.daemon_threads = True
        self.sd_srv = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.sd_state = f"Bereit – das Plugin kann sich verbinden (Port {SD_PORT})"

    def stop_sd_server(self):
        srv, self.sd_srv = self.sd_srv, None
        if srv:
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
        self.sd_state = "Aus – das Programm reagiert nicht auf das Stream Deck"

    def on_sd_opt(self):
        on = bool(self.sd_var.get())
        self.settings["streamdeck"] = on
        self.save()
        if on:
            self.start_sd_server()
        else:
            self.stop_sd_server()
        self.sd_status_lbl.configure(text=self.sd_state)

    @staticmethod
    def bundled_plugin():
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, SD_PLUGIN_FILE)
        return path if os.path.exists(path) else None

    def install_plugin(self):
        src = self.bundled_plugin()
        if not src:
            return
        try:
            import shutil
            import tempfile
            dst = os.path.join(tempfile.gettempdir(), SD_PLUGIN_FILE)
            shutil.copyfile(src, dst)
            os.startfile(dst)  # Stream Deck Software übernimmt die Installation
            self.flash("Stream Deck installiert das Plugin …")
        except Exception as e:
            self.flash(f"Plugin-Installation fehlgeschlagen: {e}")

    # ------------------------------------------------------------ Hotkeys
    def start_hotkeys(self):
        if pkb is None:
            return
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None
        st = self.settings
        mapping = {}
        for key, ev in ((st["hk_clip"], "clip"), (st["hk_gap"], "gap"), (st["hk_clock"], "clock")):
            if key:
                mapping[key] = (lambda e=ev: self.events.put(e))
        try:
            self.listener = pkb.GlobalHotKeys(mapping)
            self.listener.daemon = True
            self.listener.start()
        except Exception as e:
            messagebox.showwarning("Hotkeys", f"Hotkeys konnten nicht gestartet werden:\n{e}\n\n"
                                              f"Prüf die Schreibweise unter Einstellungen.")

    def poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if isinstance(ev, tuple) and ev[0] == "chat_msg":
                    if ev[1] == self.chat_channel:
                        self.chat_buf.append(ev)
                elif isinstance(ev, tuple):
                    self.handle_tw_event(ev)
                else:
                    {"clip": self.clip_now, "gap": self.gap_now, "clock": self.toggle_clock}[ev]()
        except queue.Empty:
            pass
        if self.chat_buf:
            self.flush_chat()
        self.root.after(80, self.poll_events)

    # ------------------------------------------------------------ Aktionen
    def need_stream(self):
        s = self.cur()
        if not s:
            self.flash("Erst einen Stream anlegen.")
        return s

    def on_select_stream(self, e=None):
        idx = self.stream_cb.current()
        if 0 <= idx < len(self.state["streams"]):
            self.state["current"] = self.state["streams"][idx]["id"]
            self.save()
            self.render()

    def toggle_clock(self):
        s = self.need_stream()
        if not s:
            return
        if s.get("tw"):
            s["tw"]["autoClock"] = False
        c = s.setdefault("clock", {"base": 0, "startedAt": None})
        if c.get("startedAt"):
            c["base"] = self.clock_now(s)
            c["startedAt"] = None
            self.flash("Uhr pausiert")
        else:
            c["startedAt"] = now_ms()
            self.flash("Uhr läuft")
        self.beep("clock")
        self.save()
        self.update_clock()

    def set_time(self):
        s = self.need_stream()
        if not s:
            return
        v = self.ask_string("Zeit setzen", "Aktuelle Zeit im Stream:", fmt(self.clock_now(s)))
        if v is None:
            return
        t = parse_time(v)
        if t is None:
            self.flash("Zeit nicht erkannt – z. B. 1:23:45")
            return
        if s.get("tw"):
            s["tw"]["autoClock"] = False
        s["clock"]["base"] = t
        s["clock"]["startedAt"] = now_ms() if self.running(s) else None
        self.save()
        self.render()

    def clip_now(self):
        s = self.need_stream()
        if not s:
            return
        t = int(self.clock_now(s))
        s["clips"].append({"id": uid(), "t": t, "note": "", "done": False})
        self.save()
        self.render()
        self.beep("clip")
        self.flash(f"Clip bei {fmt(t)} gemerkt")

    def gap_now(self):
        s = self.need_stream()
        if not s:
            return
        t = int(self.clock_now(s))
        if s.get("openGap") is None:
            s["openGap"] = t
            self.beep("gap_on")
            self.flash(f"Weg ab {fmt(t)}")
        else:
            a = s["openGap"]
            if t > a:
                s["gaps"].append({"id": uid(), "from": a, "to": t, "note": "", "done": False})
            s["openGap"] = None
            self.beep("gap_off")
            self.flash(f"Wieder da – {fmt(a)} bis {fmt(t)} gemerkt")
        self.save()
        self.render()

    def add_clip(self):
        s = self.need_stream()
        if not s:
            return
        t = parse_time(self.val(self.e_clip_t))
        if t is None:
            self.flash("Zeit nicht erkannt – z. B. 1:23:45 oder 83")
            self.e_clip_t.focus_set()
            return
        s["clips"].append({"id": uid(), "t": t, "note": self.val(self.e_clip_n), "done": False})
        self.clear(self.e_clip_n)
        self.e_clip_t.delete(0, "end")
        self.e_clip_t.focus_set()
        self.save()
        self.render()

    def add_gap(self):
        s = self.need_stream()
        if not s:
            return
        a, b = parse_time(self.val(self.e_gap_a)), parse_time(self.val(self.e_gap_b))
        if a is None or b is None:
            self.flash("Von und bis brauchen eine Zeit")
            return
        if b <= a:
            self.flash("„bis“ muss nach „von“ liegen")
            return
        s["gaps"].append({"id": uid(), "from": a, "to": b, "note": self.val(self.e_gap_n), "done": False})
        self.clear(self.e_gap_n)
        self.clear(self.e_gap_b)
        self.e_gap_a.delete(0, "end")
        self.e_gap_a.focus_set()
        self.save()
        self.render()

    def selected(self, kind):
        s = self.cur()
        tree = self.clip_tree if kind == "c" else self.gap_tree
        sel = tree.selection()
        if not s or not sel:
            return None, None
        arr = s["clips"] if kind == "c" else s["gaps"]
        for it in arr:
            if it["id"] == sel[0]:
                return arr, it
        return None, None

    def toggle_done(self, kind):
        arr, it = self.selected(kind)
        if not it:
            self.flash("Erst einen Eintrag auswählen.")
            return
        it["done"] = not it.get("done")
        self.save()
        self.render()
        (self.clip_tree if kind == "c" else self.gap_tree).selection_set(it["id"])

    def edit_note(self, kind):
        arr, it = self.selected(kind)
        if not it:
            self.flash("Erst einen Eintrag auswählen.")
            return
        v = self.ask_string("Notiz", "Notiz:", it.get("note", ""))
        if v is not None:
            it["note"] = v.strip()
            self.save()
            self.render()

    def delete_item(self, kind):
        arr, it = self.selected(kind)
        if not it:
            return
        arr.remove(it)
        self.save()
        self.render()

    def open_vod(self, kind):
        s = self.cur()
        arr, it = self.selected(kind)
        if not it:
            self.flash("Erst einen Eintrag auswählen.")
            return
        sec = max(0, it["t"] - 30) if kind == "c" else it["from"]
        link = vod_link(s.get("url"), sec)
        if not link:
            self.flash("Kein VOD-Link hinterlegt – unter „Bearbeiten“ eintragen.")
            return
        webbrowser.open(link)

    def context_menu(self, e, kind):
        tree = self.clip_tree if kind == "c" else self.gap_tree
        row = tree.identify_row(e.y)
        if not row:
            return
        tree.selection_set(row)
        m = tk.Menu(self.root, tearoff=0)
        self.style_menu(m)
        m.add_command(label="Abhaken", command=lambda: self.toggle_done(kind))
        m.add_command(label="Notiz bearbeiten", command=lambda: self.edit_note(kind))
        m.add_command(label="Im VOD öffnen", command=lambda: self.open_vod(kind))
        m.add_separator()
        m.add_command(label="Löschen", command=lambda: self.delete_item(kind))
        m.tk_popup(e.x_root, e.y_root)

    # ------------------------------------------------------------ Twitch: Oberfläche
    def build_sidebar(self, body):
        side = ttk.Frame(body, width=350)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        head = ttk.Frame(side)
        head.pack(fill="x", pady=(0, 8))
        self.live_dot = tk.Canvas(head, width=12, height=12, highlightthickness=0, bd=0)
        self.live_dot.pack(side="left", padx=(0, 8))
        ttk.Label(head, text="Live auf Twitch", style="H.TLabel").pack(side="left")
        self.tw_status = ttk.Label(head, text="", style="Muted.TLabel")
        self.tw_status.pack(side="right")

        # nicht angemeldet
        self.tw_login_fr = ttk.Frame(side)
        ttk.Label(self.tw_login_fr, wraplength=330, justify="left", style="Stats.TLabel",
                  text="Melde dich mit Twitch an, dann siehst du hier, wer von deinen gefolgten Kanälen "
                       "gerade live ist. Ein Klick legt den Stream an und die Uhr läuft automatisch "
                       "synchron zur echten Stream-Zeit.").pack(anchor="w", pady=(2, 14))
        ttk.Button(self.tw_login_fr, text="Mit Twitch anmelden", style="Accent.TButton",
                   command=self.twitch_login).pack(anchor="w")

        # angemeldet
        self.tw_live_fr = ttk.Frame(side)
        acc = ttk.Frame(self.tw_live_fr)
        acc.pack(side="bottom", fill="x", pady=(12, 0))
        self.tw_acc_lbl = ttk.Label(acc, text="", style="Muted.TLabel")
        self.tw_acc_lbl.pack(side="left")
        lo = ttk.Label(acc, text="Abmelden", style="Link.TLabel", cursor="hand2")
        lo.pack(side="right")
        lo.bind("<Button-1>", lambda e: self.twitch_logout())

        b = ttk.Frame(self.tw_live_fr)
        b.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(b, text="Stream anlegen", style="Accent.TButton",
                   command=self.create_from_selected).pack(side="left")
        ttk.Button(b, text="Öffnen", command=self.open_channel).pack(side="left", padx=6)
        ttk.Button(b, text="★", width=3, command=self.toggle_fav).pack(side="left")
        ttk.Button(b, text="Kanäle …", command=self.open_settings).pack(side="right")

        self.live_info = ttk.Label(self.tw_live_fr, text="", style="Stats.TLabel", wraplength=330, justify="left")
        self.live_info.pack(side="bottom", fill="x", pady=(8, 0))

        fr = ttk.Frame(self.tw_live_fr)
        fr.pack(fill="both", expand=True)
        t = ttk.Treeview(fr, columns=("fav", "name", "game", "up"), show="headings", selectmode="browse")
        for c, h, w, stretch in (("fav", "", 34, False), ("name", "Kanal", 120, False),
                                 ("game", "Spiel", 90, True), ("up", "Live seit", 74, False)):
            t.heading(c, text=h, anchor="w")
            t.column(c, width=w, minwidth=w if not stretch else 60, stretch=stretch,
                     anchor="center" if c == "fav" else "w")
        sb = ttk.Scrollbar(fr, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        t.bind("<Double-1>", lambda e: self.create_from_selected() if t.identify_column(e.x) != "#1" else None)
        t.bind("<ButtonRelease-1>", self.on_live_click)
        t.bind("<Button-3>", self.live_context)
        t.bind("<<TreeviewSelect>>", lambda e: (self.update_live_info(), self.update_chat_target()))
        self.live_tree = t

    def update_account_ui(self):
        if self.tw.logged_in:
            self.tw_login_fr.pack_forget()
            self.tw_live_fr.pack(fill="both", expand=True)
            self.tw_acc_lbl.configure(text=f"Angemeldet als {self.tw.name}")
        else:
            self.tw_live_fr.pack_forget()
            self.tw_login_fr.pack(fill="x")
            self.tw_status.configure(text="")
        if self.tw.logged_in:
            self.set_acc_lbl.configure(text=f"Angemeldet als {self.tw.name}")
            self.set_acc_btn.configure(text="Abmelden", command=self.twitch_logout, style="TButton")
        else:
            self.set_acc_lbl.configure(text="Nicht angemeldet")
            self.set_acc_btn.configure(text="Mit Twitch anmelden", command=self.twitch_login,
                                       style="Accent.TButton")
        self.update_chat_input()
        self.draw_timeline()

    def live_by_uid(self, user_id):
        for x in self.live:
            if x["user_id"] == user_id:
                return x
        return None

    def selected_live(self):
        sel = self.live_tree.selection()
        return self.live_by_uid(sel[0]) if sel else None

    def render_live(self):
        self.last_live_render = time.time()
        t = self.live_tree
        sel = t.selection()
        t.delete(*t.get_children())
        favs = set(self.settings.get("favs", []))
        linked = {str(s["tw"].get("stream_id")) for s in self.state["streams"] if s.get("tw")}
        items = sorted(self.live, key=lambda x: (x["user_id"] not in favs, -x.get("viewer_count", 0)))
        for x in items:
            up = int((now_ms() - iso_ms(x["started_at"])) / 1000)
            fav = x["user_id"] in favs
            tags = ("linked",) if str(x["id"]) in linked else (("fav",) if fav else ())
            t.insert("", "end", iid=x["user_id"], tags=tags,
                     values=("★" if fav else "☆", x.get("user_name", ""), x.get("game_name", ""),
                             f"{up // 3600}:{up % 3600 // 60:02d} h"))
        if sel and t.exists(sel[0]):
            t.selection_set(sel[0])
        self.update_live_info()

    def update_live_info(self):
        x = self.selected_live()
        if not self.live:
            txt = "Gerade ist keiner live, dem du folgst."
        elif x:
            txt = (x.get("title") or "").strip() or "(kein Titel)"
            if any(s.get("tw", {}).get("stream_id") == x["id"] for s in self.state["streams"]):
                txt += "\n\nSchon angelegt – Doppelklick springt hin."
        else:
            n = len(self.live)
            txt = f"{n} {'Kanal' if n == 1 else 'Kanäle'} live. Doppelklick legt den Stream an, Klick auf ☆ macht ihn zum Favoriten."
        self.live_info.configure(text=txt)

    def on_live_click(self, e):
        t = self.live_tree
        row = t.identify_row(e.y)
        if row and t.identify_column(e.x) == "#1":
            t.selection_set(row)
            self.toggle_fav()

    def live_context(self, e):
        row = self.live_tree.identify_row(e.y)
        if not row:
            return
        self.live_tree.selection_set(row)
        m = tk.Menu(self.root, tearoff=0)
        self.style_menu(m)
        m.add_command(label="Stream anlegen", command=self.create_from_selected)
        m.add_command(label="Auf Twitch öffnen", command=self.open_channel)
        m.add_command(label="Favorit an/aus", command=self.toggle_fav)
        m.add_separator()
        m.add_command(label="Aus der Liste ausblenden", command=self.hide_selected)
        m.tk_popup(e.x_root, e.y_root)

    def toggle_fav(self):
        x = self.selected_live()
        if not x:
            self.flash("Erst einen Kanal auswählen.")
            return
        favs = self.settings.setdefault("favs", [])
        if x["user_id"] in favs:
            favs.remove(x["user_id"])
        else:
            favs.append(x["user_id"])
        self.save()
        self.render_live()

    def open_channel(self):
        x = self.selected_live()
        if x:
            webbrowser.open("https://www.twitch.tv/" + x.get("user_login", ""))

    def create_from_selected(self):
        x = self.selected_live()
        if not x:
            self.flash("Erst einen Kanal auswählen.")
            return
        self.create_from_live(x)

    def create_from_live(self, x):
        for s in self.state["streams"]:
            if s.get("tw", {}).get("stream_id") == x["id"]:
                self.state["current"] = s["id"]
                self.save()
                self.render()
                self.render_live()
                return
        st_ms = iso_ms(x["started_at"])
        date = time.strftime("%d.%m.", time.localtime(st_ms / 1000))
        title = (x.get("title") or "").strip()
        short = (title[:42] + "…") if len(title) > 42 else title
        name = f"{x.get('user_name')} {date}" + (f" – {short}" if short else "")
        ns = {"id": uid(), "name": name, "url": "", "length": 0, "clips": [], "gaps": [], "openGap": None,
              "clock": {"base": 0, "startedAt": st_ms}, "created": now_ms(),
              "tw": {"user_id": x["user_id"], "login": x.get("user_login"), "display": x.get("user_name"),
                     "stream_id": x["id"], "started_ms": st_ms, "autoClock": True, "lastSeen": now_ms()}}
        self.state["streams"].insert(0, ns)
        self.state["current"] = ns["id"]
        self.save()
        self.render()
        self.render_live()
        self.flash(f"{x.get('user_name')} angelegt – Uhr läuft synchron")
        self.root.after(1500, self.twitch_poll)  # VOD-Link gleich nachholen

    # ------------------------------------------------------------ Twitch: Anmelden
    def twitch_login(self):
        if not self.tw.configured:
            messagebox.showinfo("Twitch", "In dieser Version ist noch keine Twitch-Client-ID eingetragen.\n\n"
                                          "Trag sie in stream_merkzettel.py bei TWITCH_CLIENT_ID ein "
                                          "und bau die exe neu.")
            return
        d, f = self.new_dialog("Mit Twitch anmelden")
        ttk.Label(f, text="Im Browser öffnet sich Twitch. Prüf, ob dort dieser Code steht, und bestätige:",
                  wraplength=380, justify="left").pack(anchor="w")
        code = ttk.Label(f, text="…", font=(FONT, 30, "bold"))
        code.pack(pady=16)
        status = ttk.Label(f, text="Code wird geholt …", style="Stats.TLabel", wraplength=380, justify="left")
        status.pack(anchor="w")
        state = {"cancel": False, "uri": None}

        def open_uri():
            if state["uri"]:
                webbrowser.open(state["uri"])

        def cancel():
            state["cancel"] = True
            self.login_ui = None
            d.destroy()

        b = ttk.Frame(f)
        b.pack(anchor="e", pady=(18, 0))
        ttk.Button(b, text="Abbrechen", command=cancel).pack(side="left", padx=6)
        ttk.Button(b, text="Twitch nochmal öffnen", style="Accent.TButton", command=open_uri).pack(side="left")
        d.protocol("WM_DELETE_WINDOW", cancel)
        d.bind("<Escape>", lambda e: cancel())
        self.login_ui = {"dlg": d, "code": code, "status": status, "state": state}
        self.center(d)

        def worker():
            try:
                dev = self.tw.start_device()
            except Exception as e:
                self.events.put(("login_err", f"Twitch nicht erreichbar: {e}"))
                return
            self.events.put(("login_code", dev))
            interval = max(int(dev.get("interval", 5)), 2)
            deadline = time.time() + int(dev.get("expires_in", 1800))
            while not state["cancel"] and time.time() < deadline:
                time.sleep(interval)
                if state["cancel"]:
                    return
                try:
                    if self.tw.poll_device(dev["device_code"]):
                        self.events.put(("login_ok",))
                        return
                except Exception as e:
                    self.events.put(("login_err", f"Anmeldung abgebrochen: {e}"))
                    return
            if not state["cancel"]:
                self.events.put(("login_err", "Der Code ist abgelaufen. Mach das Fenster zu und probier's nochmal."))

        threading.Thread(target=worker, daemon=True).start()

    def twitch_logout(self):
        if not messagebox.askyesno("Twitch", "Von Twitch abmelden?"):
            return
        threading.Thread(target=self.tw.logout, daemon=True).start()
        self.tw.tok = {}
        self.live, self.live_ids = [], None
        self.live_tree.delete(*self.live_tree.get_children())
        self.update_account_ui()

    # ------------------------------------------------------------ Twitch: Abfragen
    def twitch_poll(self):
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
        self.poll_job = self.root.after(POLL_SECONDS * 1000, self.twitch_poll)
        if not self.tw.logged_in or self.polling:
            return
        self.polling = True
        jobs = [(s["id"], s["tw"]["user_id"], s["tw"]["stream_id"]) for s in self.state["streams"]
                if s.get("tw") and not s.get("url") and s["tw"].get("stream_id")][:10]
        extra = [c["id"] for c in self.settings.get("extra", [])]
        threading.Thread(target=self._poll_worker, args=(jobs, extra), daemon=True).start()

    def _poll_worker(self, jobs, extra=()):
        try:
            live = self.tw.followed_live()
            have = {x["user_id"] for x in live}
            missing = [i for i in extra if i not in have]
            if missing:
                live += self.tw.streams_by_ids(missing)
            self.events.put(("live", live))
            for sid, user_id, stream_id in jobs:
                try:
                    url = self.tw.find_vod(user_id, stream_id)
                    if url:
                        self.events.put(("vod", sid, url))
                except AuthLost:
                    raise
                except Exception:
                    pass
        except AuthLost:
            self.events.put(("auth_lost",))
        except Exception as e:
            self.events.put(("poll_err", str(e)))
        finally:
            self.events.put(("poll_done",))

    def handle_tw_event(self, ev):
        kind = ev[0]
        if kind == "live":
            self.on_live_data(ev[1])
        elif kind == "vod":
            for s in self.state["streams"]:
                if s["id"] == ev[1] and not s.get("url"):
                    s["url"] = ev[2]
                    self.save()
                    self.render()
        elif kind == "poll_done":
            self.polling = False
        elif kind == "chat_sys":
            if ev[1] == self.chat_channel:
                self.chat_append_sys(ev[2])
        elif kind == "chan_found":
            self.on_channel_found(ev[1], ev[2])
        elif kind == "chat_room":
            if ev[1] == self.chat_channel:
                self.chat_room_id = ev[2]
                self.update_chat_input()
                if ev[1] not in self.emote_maps:
                    chan, rid = ev[1], ev[2]
                    threading.Thread(target=lambda: self.events.put(
                        ("emote_map", chan, third_party_emotes(rid))), daemon=True).start()
        elif kind == "emote_map":
            self.emote_maps[ev[1]] = ev[2]
        elif kind == "emote_img":
            self.on_emote_img(ev[1], ev[2])
        elif kind == "shortcut":
            self.sc_status.configure(text=ev[2])
            self.flash(ev[2])
        elif kind == "chat_sent":
            if not ev[1]:
                self.chat_append_sys("Nicht gesendet: " + ev[2])
        elif kind == "poll_err":
            self.tw_status.configure(text="keine Verbindung")
        elif kind == "auth_lost":
            self.live, self.live_ids = [], None
            self.update_account_ui()
            self.flash("Twitch-Anmeldung abgelaufen – bitte neu anmelden.")
        elif kind == "login_code" and self.login_ui:
            dev = ev[1]
            self.login_ui["code"].configure(text=dev.get("user_code", "?"))
            self.login_ui["status"].configure(text="Warte auf deine Bestätigung bei Twitch …")
            self.login_ui["state"]["uri"] = dev.get("verification_uri")
            webbrowser.open(dev.get("verification_uri"))
        elif kind == "login_err" and self.login_ui:
            self.login_ui["status"].configure(text=ev[1], foreground=self.pal["danger"])
        elif kind == "login_ok":
            if self.login_ui:
                self.login_ui["dlg"].destroy()
                self.login_ui = None
            self.update_account_ui()
            self.flash(f"Mit Twitch verbunden als {self.tw.name}")
            self.twitch_poll()

    def on_live_data(self, live):
        now = now_ms()
        all_by_id = {x["user_id"]: x for x in live}
        hidden = {h["id"] for h in self.settings.get("hidden", [])}
        live = [x for x in live if x["user_id"] not in hidden]
        by_id = {x["user_id"]: x for x in live}
        if self.live_ids is not None:
            mode = self.settings.get("notify", "all")
            favs = set(self.settings.get("favs", []))
            for user_id in set(by_id) - self.live_ids:
                if mode == "all" or (mode == "favs" and user_id in favs):
                    self.notify_live(by_id[user_id])
        self.live_ids = set(by_id)
        self.live = live

        changed = False
        for s in self.state["streams"]:
            tw = s.get("tw")
            if not tw:
                continue
            x = all_by_id.get(tw["user_id"])
            if x and str(x["id"]) == str(tw.get("stream_id")):
                tw["lastSeen"] = now
                if tw.get("autoClock"):
                    s["clock"] = {"base": 0, "startedAt": iso_ms(x["started_at"])}
                changed = True
            elif tw.get("autoClock") and tw.get("lastSeen"):
                end = max(0, (tw["lastSeen"] - tw["started_ms"]) / 1000)
                s["clock"] = {"base": end, "startedAt": None}
                tw["autoClock"] = False
                if not s.get("length"):
                    s["length"] = int(end)
                changed = True
                self.flash(f"Stream von {tw.get('display')} ist vorbei – Uhr gestoppt")
        if changed:
            self.save()
            self.render()
        self.render_live()
        self.update_chat_target()
        self.tw_status.configure(text="aktualisiert " + time.strftime("%H:%M"))

    def notify_live(self, x):
        self.beep("live")
        p = self.pal
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        fr = tk.Frame(w, bg=p["field"], padx=16, pady=14, highlightthickness=1,
                      highlightbackground=p["track"], highlightcolor=p["track"])
        fr.pack(fill="both", expand=True)
        top = tk.Frame(fr, bg=p["field"])
        top.pack(fill="x")
        tk.Label(top, text="●  LIVE", bg=p["field"], fg=p["live"], font=(FONT, 9, "bold")).pack(side="left")
        close = tk.Label(top, text="✕", bg=p["field"], fg=p["muted"], cursor="hand2", font=(FONT, 11))
        close.pack(side="right")
        tk.Label(fr, text=x.get("user_name", ""), bg=p["field"], fg=p["fg"], font=(FONT, 14, "bold"),
                 anchor="w").pack(fill="x", pady=(4, 0))
        title = (x.get("title") or "").strip()
        if title:
            tk.Label(fr, text=title, bg=p["field"], fg=p["fg"], font=(FONT, 10), wraplength=300,
                     justify="left", anchor="w").pack(fill="x")
        if x.get("game_name"):
            tk.Label(fr, text=x["game_name"], bg=p["field"], fg=p["muted"], font=(FONT, 9),
                     anchor="w").pack(fill="x", pady=(2, 0))
        act = tk.Frame(fr, bg=p["field"])
        act.pack(fill="x", pady=(10, 0))

        def done():
            if w in self.toasts:
                self.toasts.remove(w)
            if w.winfo_exists():
                w.destroy()

        def take():
            self.create_from_live(x)
            self.root.deiconify()
            self.root.lift()
            done()

        btn = tk.Label(act, text="Stream anlegen", bg=p["gap"], fg="#ffffff", padx=12, pady=5,
                       cursor="hand2", font=(FONT, 10, "bold"))
        btn.pack(side="left")
        btn.bind("<Button-1>", lambda e: take())
        tw_btn = tk.Label(act, text="Ansehen", bg=p["field"], fg=p["gap"], padx=10, pady=5,
                          cursor="hand2", font=(FONT, 10))
        tw_btn.pack(side="left", padx=4)
        tw_btn.bind("<Button-1>", lambda e: (webbrowser.open("https://www.twitch.tv/" + x.get("user_login", "")), done()))
        close.bind("<Button-1>", lambda e: done())

        w.update_idletasks()
        ww, wh = max(w.winfo_reqwidth(), 340), w.winfo_reqheight()
        offset = sum(t.winfo_height() + 10 for t in self.toasts if t.winfo_exists())
        sx, sy = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"{ww}x{wh}+{sx - ww - 24}+{sy - wh - 64 - offset}")
        self.toasts.append(w)
        w.after(12000, done)

    # ------------------------------------------------------------ Chat
    def build_chat(self, body):
        self.chat_fr = ttk.Frame(body, width=340)
        self.chat_fr.pack_propagate(False)
        self.chat_sep = ttk.Separator(body, orient="vertical")
        head = ttk.Frame(self.chat_fr)
        head.pack(fill="x", pady=(0, 8))
        ttk.Label(head, text="Chat", style="H.TLabel").pack(side="left")
        self.chat_chan_lbl = ttk.Label(head, text="", style="Muted.TLabel")
        self.chat_chan_lbl.pack(side="left", padx=(10, 0))
        x = ttk.Label(head, text="ausblenden", style="Link.TLabel", cursor="hand2")
        x.pack(side="right")
        x.bind("<Button-1>", lambda e: self.toggle_chat())
        fr = ttk.Frame(self.chat_fr)
        self.chat_fr_text = fr
        self.chat_txt = tk.Text(fr, wrap="word", relief="flat", bd=0, highlightthickness=0, padx=4, pady=4,
                                font=(FONT, 10), spacing1=2, spacing3=3, cursor="arrow", state="disabled")
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.chat_txt.yview)
        self.chat_txt.configure(yscrollcommand=sb.set)
        self.chat_txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        bottom = ttk.Frame(self.chat_fr)
        bottom.pack(side="bottom", fill="x")
        self.chat_entry_fr = ttk.Frame(bottom)
        self.chat_entry = ttk.Entry(self.chat_entry_fr, font=(FONT, 10))
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", self.send_chat)
        ttk.Button(self.chat_entry_fr, text="Senden", style="Accent.TButton",
                   command=self.send_chat).pack(side="left", padx=(6, 0))
        self.chat_hint = ttk.Label(bottom, text="", style="Link.TLabel", cursor="hand2")
        self.chat_hint_cmd = None
        self.chat_hint.bind("<Button-1>", lambda e: self.chat_hint_cmd and self.chat_hint_cmd())
        fr.pack(fill="both", expand=True)

    def toggle_chat(self):
        self.settings["chat"] = not self.settings.get("chat", True)
        self.save()
        self.apply_chat_visibility()

    def apply_chat_visibility(self, initial=False):
        on = self.settings.get("chat", True)
        shown = bool(self.chat_fr.winfo_manager())
        extra = 340 + 44
        self.chat_btn.configure(text="Chat aus" if on else "Chat an")
        if on and not shown:
            self.chat_fr.pack(side="right", fill="y", before=self.main_fr)
            self.chat_sep.pack(side="right", fill="y", padx=22, before=self.main_fr)
        elif not on and shown:
            self.chat_fr.pack_forget()
            self.chat_sep.pack_forget()
        self.root.minsize(1240 + (extra if on else 0), 720)
        if self.root.state() != "zoomed" and (initial and on or shown != on and not initial):
            self.root.update_idletasks()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if initial:
                w = 1460
                h = 860
            w = w + extra if on else w - extra
            w = min(max(w, 1240), self.root.winfo_screenwidth() - 40)
            self.root.geometry(f"{w}x{h}")
        self.update_chat_target()

    def update_chat_target(self):
        target = None
        if self.settings.get("chat", True):
            s = self.cur()
            if s and s.get("tw", {}).get("login"):
                target = s["tw"]["login"]
            else:
                x = self.selected_live()
                if x:
                    target = x.get("user_login")
        target = target.lower() if target else None
        if target == self.chat_channel:
            return
        self.chat_channel = target
        self.chat_room_id = None
        self.chat_buf.clear()
        t = self.chat_txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.configure(state="disabled")
        self.chat_chan_lbl.configure(text=f"#{target}" if target else "")
        self.chat.set_channel(target)
        if not target and self.settings.get("chat", True):
            self.chat_append_sys("Wähl links einen Live-Kanal aus oder leg einen Stream von Twitch an, "
                                 "dann erscheint hier der Chat.")
        elif target:
            self.chat_append_sys(f"Verbinde mit #{target} …")
        self.update_chat_input()

    def chat_append_sys(self, text):
        t = self.chat_txt
        t.configure(state="normal")
        t.insert("end", text + "\n", "sys")
        t.configure(state="disabled")
        t.see("end")

    def flush_chat(self):
        t = self.chat_txt
        at_bottom = t.yview()[1] > 0.98
        t.configure(state="normal")
        for ev in self.chat_buf[-300:]:
            _, chan, name, color, msg, emotes = ev
            color = self.readable(color)
            tag = "u" + color.lstrip("#").lower()
            if tag not in self.chat_tags:
                t.tag_configure(tag, foreground=color, font=(FONT, 10, "bold"))
                self.chat_tags.add(tag)
            t.insert("end", name, tag)
            t.insert("end", ": ")
            for seg in self.chat_segments(msg, emotes, chan):
                if seg[0] == "img":
                    t.image_create("end", image=self.emote_image(seg[1], seg[2]), padx=1)
                else:
                    t.insert("end", seg[1])
            t.insert("end", "\n")
        self.chat_buf.clear()
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 600:
            t.delete("1.0", f"{lines - 500}.0")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")

    def readable(self, color):
        """Zu dunkle Namen im Darkmode (bzw. zu helle im Hellmodus) lesbar machen."""
        try:
            r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        except Exception:
            return self.pal["fg"]
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        if self.settings.get("theme") == "dark" and lum < 0.45:
            f = 0.5
            r, g, b = (int(c + (255 - c) * f) for c in (r, g, b))
        elif self.settings.get("theme") == "light" and lum > 0.7:
            f = 0.45
            r, g, b = (int(c * (1 - f)) for c in (r, g, b))
        return f"#{r:02x}{g:02x}{b:02x}"

    def chat_segments(self, msg, emotes, chan):
        if not self.settings.get("emotes", True):
            return [("txt", msg)]
        ranges = []
        for part in (emotes or "").split("/"):
            eid, _, pos = part.partition(":")
            for r in pos.split(","):
                a, _, b = r.partition("-")
                if eid and a.isdigit() and b.isdigit():
                    ranges.append((int(a), int(b), eid))
        ranges.sort()
        out, i = [], 0
        for a, b, eid in ranges:
            if a < i or b >= len(msg):
                continue
            if a > i:
                out += self.split_words(msg[i:a], chan)
            out.append(("img", "tw:" + eid,
                        f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/static/dark/1.0"))
            i = b + 1
        out += self.split_words(msg[i:], chan)
        return out

    def split_words(self, text, chan):
        own, glob = self.emote_maps.get(chan, {}), self.emote_maps.get("global", {})
        if not own and not glob:
            return [("txt", text)]
        res, buf = [], ""
        for tok in re.split(r"(\s+)", text):
            hit = own.get(tok) or glob.get(tok)
            if hit:
                if buf:
                    res.append(("txt", buf))
                    buf = ""
                res.append(("img", hit[0], hit[1]))
            else:
                buf += tok
        if buf:
            res.append(("txt", buf))
        return res

    def emote_image(self, key, url):
        img = self.emote_imgs.get(key)
        if img is None:
            img = tk.PhotoImage(width=28, height=28)   # Platzhalter, wird nach dem Laden ersetzt
            self.emote_imgs[key] = img
            self.dl_q.put((key, url))
        return img

    def _emote_worker(self):
        while True:
            key, url = self.dl_q.get()
            try:
                self.events.put(("emote_img", key, download_bytes(url)))
            except Exception:
                pass

    def on_emote_img(self, key, data):
        img = self.emote_imgs.get(key)
        if img is None:
            return
        try:
            new = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
            img.configure(width=new.width(), height=new.height())
            img.blank()
            img.tk.call(str(img), "copy", str(new))
        except Exception:
            pass

    # --- Schreiben
    def update_chat_input(self):
        if not hasattr(self, "chat_entry"):
            return
        for w in (self.chat_entry_fr, self.chat_hint):
            w.pack_forget()
        if self.tw.can_write and self.chat_channel:
            self.chat_entry_fr.pack(fill="x", pady=(8, 0))
            self.chat_entry.configure(state="normal" if self.chat_room_id else "disabled")
        else:
            if not self.chat_channel:
                txt, link = "", None
            elif not self.tw.logged_in:
                txt, link = "Zum Schreiben mit Twitch anmelden", self.twitch_login
            else:
                txt, link = "Zum Schreiben einmal neu mit Twitch anmelden", self.twitch_login
            self.chat_hint.configure(text=txt)
            self.chat_hint_cmd = link
            if txt:
                self.chat_hint.pack(anchor="w", pady=(8, 0))

    def send_chat(self, event=None):
        msg = self.chat_entry.get().strip()
        if not msg or not self.chat_room_id or not self.tw.can_write:
            return
        self.chat_entry.delete(0, "end")
        rid = self.chat_room_id

        def worker():
            try:
                ok, reason = self.tw.send_chat(rid, msg[:500])
            except Exception as e:
                ok, reason = False, str(e)
            self.events.put(("chat_sent", ok, reason))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ Kanäle ausblenden / hinzufügen
    def hide_selected(self):
        x = self.selected_live()
        if not x:
            return
        hidden = self.settings.setdefault("hidden", [])
        if not any(h["id"] == x["user_id"] for h in hidden):
            hidden.append({"id": x["user_id"], "name": x.get("user_name", "")})
        # zusätzliche Kanäle beim Ausblenden gleich mit entfernen
        self.settings["extra"] = [c for c in self.settings.get("extra", []) if c["id"] != x["user_id"]]
        self.save()
        self.live = [y for y in self.live if y["user_id"] != x["user_id"]]
        if self.live_ids:
            self.live_ids.discard(x["user_id"])
        self.render_live()
        self.update_chat_target()
        self.refresh_manage()
        self.flash(f"{x.get('user_name')} ausgeblendet – in den Einstellungen wieder einblenden")

    def manage_channels(self):
        self.open_settings()

    def refresh_manage(self):
        ui = self.manage_ui
        if not ui:
            return
        for key, items in (("extra", self.settings.get("extra", [])), ("hidden", self.settings.get("hidden", []))):
            t = ui[key]
            t.delete(*t.get_children())
            for it in items:
                t.insert("", "end", iid=it["id"], values=(it.get("name") or it.get("login") or it["id"],))
            if not items:
                t.insert("", "end", iid="_leer", values=("– keine –",))

    def add_channel(self, name):
        name = (name or "").strip().lstrip("@")
        name = name.rstrip("/").split("/")[-1]  # erlaubt auch twitch.tv/name
        ui = self.manage_ui
        if not name:
            return
        if not self.tw.logged_in:
            ui["status"].configure(text="Dafür musst du mit Twitch angemeldet sein.")
            return
        ui["status"].configure(text=f"Suche „{name}“ …")

        def worker():
            try:
                self.events.put(("chan_found", name, self.tw.user_by_login(name)))
            except Exception as ex:
                self.events.put(("chan_found", name, str(ex)))

        threading.Thread(target=worker, daemon=True).start()

    def on_channel_found(self, name, u):
        ui = self.manage_ui
        status = ui["status"] if ui else None
        if not isinstance(u, dict):
            if status:
                status.configure(text=f"„{name}“ nicht gefunden." if u is None else f"Fehler: {u}")
            return
        extra = self.settings.setdefault("extra", [])
        if not any(c["id"] == u["id"] for c in extra):
            extra.append({"id": u["id"], "login": u.get("login"), "name": u.get("display_name")})
        self.settings["hidden"] = [h for h in self.settings.get("hidden", []) if h["id"] != u["id"]]
        self.save()
        self.refresh_manage()
        if status:
            status.configure(text=f"{u.get('display_name')} hinzugefügt.")
            ui["entry"].delete(0, "end")
        self.twitch_poll()

    def remove_extra(self, t):
        sel = t.selection()
        if not sel or sel[0] == "_leer":
            return
        self.settings["extra"] = [c for c in self.settings.get("extra", []) if c["id"] != sel[0]]
        self.save()
        self.refresh_manage()
        self.twitch_poll()

    def unhide(self, t):
        sel = t.selection()
        if not sel or sel[0] == "_leer":
            return
        self.settings["hidden"] = [h for h in self.settings.get("hidden", []) if h["id"] != sel[0]]
        self.save()
        self.refresh_manage()
        self.twitch_poll()

    # ------------------------------------------------------------ Dialoge
    def new_dialog(self, title):
        d = tk.Toplevel(self.root)
        d.title(title)
        d.transient(self.root)
        d.resizable(False, False)
        d.configure(bg=self.pal["bg"])
        self.dialogs.append(d)
        dark_titlebar(d, self.settings.get("theme") == "dark")
        f = ttk.Frame(d, padding=20)
        f.pack(fill="both", expand=True)
        return d, f

    def center(self, d):
        d.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - d.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - d.winfo_height()) // 3
        d.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        d.grab_set()

    def ask_string(self, title, label, initial=""):
        d, f = self.new_dialog(title)
        ttk.Label(f, text=label).pack(anchor="w")
        e = ttk.Entry(f, width=44, font=(FONT, 10))
        e.insert(0, initial)
        e.pack(fill="x", pady=(6, 14))
        result = {"v": None}

        def ok(event=None):
            result["v"] = e.get()
            d.destroy()

        b = ttk.Frame(f)
        b.pack(anchor="e")
        ttk.Button(b, text="Abbrechen", command=d.destroy).pack(side="left", padx=6)
        ttk.Button(b, text="OK", style="Accent.TButton", command=ok).pack(side="left")
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda ev: d.destroy())
        self.center(d)
        e.focus_set()
        e.select_range(0, "end")
        self.root.wait_window(d)
        return result["v"]

    def stream_dialog(self, edit):
        s = self.cur() if edit else None
        if edit and not s:
            self.flash("Erst einen Stream anlegen.")
            return
        d, f = self.new_dialog("Stream bearbeiten" if edit else "Neuer Stream")
        fields = {}
        for i, (key, label, init) in enumerate((
                ("name", "Name", s["name"] if s else ""),
                ("url", "VOD-Link (optional, für Sprung-Links)", s.get("url", "") if s else ""),
                ("len", "Länge (optional, z. B. 4:30:00)", fmt(s["length"]) if s and s.get("length") else ""))):
            ttk.Label(f, text=label).pack(anchor="w", pady=(10 if i else 0, 4))
            e = ttk.Entry(f, width=52, font=(FONT, 10))
            e.insert(0, init)
            e.pack(fill="x")
            fields[key] = e

        def ok(event=None):
            name = fields["name"].get().strip() or ("Stream " + time.strftime("%d.%m.%Y"))
            url = fields["url"].get().strip()
            ln = parse_time(fields["len"].get()) or 0
            if s:
                s.update(name=name, url=url, length=ln)
            else:
                ns = {"id": uid(), "name": name, "url": url, "length": ln, "clips": [], "gaps": [],
                      "openGap": None, "clock": {"base": 0, "startedAt": None}, "created": now_ms()}
                self.state["streams"].insert(0, ns)
                self.state["current"] = ns["id"]
            self.save()
            self.render()
            d.destroy()

        b = ttk.Frame(f)
        b.pack(anchor="e", pady=(18, 0))
        ttk.Button(b, text="Abbrechen", command=d.destroy).pack(side="left", padx=6)
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=ok).pack(side="left")
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda e: d.destroy())
        self.center(d)
        fields["name"].focus_set()

    def delete_stream(self):
        s = self.cur()
        if not s:
            return
        if not messagebox.askyesno("Löschen", f"„{s['name']}“ mit allen Einträgen löschen?"):
            return
        self.state["streams"] = [x for x in self.state["streams"] if x["id"] != s["id"]]
        self.state["current"] = self.state["streams"][0]["id"] if self.state["streams"] else None
        self.save()
        self.render()

    # ------------------------------------------------------------ Einstellungen (Seite im Fenster)
    def build_settings_page(self, outer):
        pg = ttk.Frame(outer)
        self.set_page = pg
        top = ttk.Frame(pg)
        top.pack(fill="x", pady=(0, 16))
        ttk.Button(top, text="←  Zurück", command=self.close_settings).pack(side="left")
        ttk.Label(top, text="Einstellungen", style="Title.TLabel").pack(side="left", padx=16)
        self.set_err = ttk.Label(top, text="", foreground=THEMES["dark"]["danger"])
        self.set_err.pack(side="right")

        cols = ttk.Frame(pg)
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1, uniform="s")
        cols.columnconfigure(2, weight=1, uniform="s")
        left = ttk.Frame(cols)
        left.grid(row=0, column=0, sticky="nsew")
        ttk.Separator(cols, orient="vertical").grid(row=0, column=1, sticky="ns", padx=28)
        right = ttk.Frame(cols)
        right.grid(row=0, column=2, sticky="nsew")

        def h(parent, text, first=False):
            ttk.Label(parent, text=text, style="H.TLabel").pack(anchor="w", pady=(0 if first else 22, 8))

        # Darstellung
        h(left, "Darstellung", True)
        self.theme_var = tk.StringVar(value=self.settings.get("theme", "dark"))
        r = ttk.Frame(left)
        r.pack(anchor="w")
        for val, lab in (("dark", "Dunkel"), ("light", "Hell")):
            ttk.Radiobutton(r, text=lab, value=val, variable=self.theme_var,
                            command=self.on_theme_change).pack(side="left", padx=(0, 18))

        # Hotkeys
        h(left, "Globale Hotkeys")
        ttk.Label(left, text="Schreibweise:  <f9>   oder   <ctrl>+<alt>+c   oder   <ctrl>+<shift>+<f2>",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        g = ttk.Frame(left)
        g.pack(anchor="w")
        self.hk_entries = {}
        for i, (k, lab) in enumerate((("hk_clip", "Clip hier"), ("hk_gap", "Bin weg / Wieder da"),
                                      ("hk_clock", "Uhr Start/Pause"))):
            ttk.Label(g, text=lab).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 16))
            e = ttk.Entry(g, width=24, font=(FONT, 10))
            e.grid(row=i, column=1, sticky="w", pady=4)
            self.hk_entries[k] = e
        self.snd_var = tk.BooleanVar()
        ttk.Checkbutton(left, text="Piepton bei Hotkey (Rückmeldung im Vollbild)", variable=self.snd_var,
                        command=lambda: self.set_opt("sound", self.snd_var.get())).pack(anchor="w", pady=(10, 0))
        if pkb is None:
            ttk.Label(left, text="pynput ist nicht installiert – Hotkeys gehen nur im Fenster.",
                      style="Stats.TLabel").pack(anchor="w", pady=(6, 0))

        # Chat
        h(left, "Programm")
        sc = ttk.Frame(left)
        sc.pack(fill="x")
        ttk.Button(sc, text="Desktop-Verknüpfung erstellen", command=self.make_shortcut).pack(side="left")
        self.sc_status = ttk.Label(sc, text="", style="Stats.TLabel")
        self.sc_status.pack(side="left", padx=12)

        h(left, "Stream Deck")
        self.sd_var = tk.BooleanVar()
        ttk.Checkbutton(left, text="Steuerung per Stream-Deck-Plugin „Clipp Helper“ erlauben",
                        variable=self.sd_var, command=self.on_sd_opt).pack(anchor="w")
        self.sd_status_lbl = ttk.Label(left, text="", style="Stats.TLabel")
        self.sd_status_lbl.pack(anchor="w", pady=(4, 0))
        if self.bundled_plugin():
            ttk.Button(left, text="Stream-Deck-Plugin installieren",
                       command=self.install_plugin).pack(anchor="w", pady=(8, 0))
        else:
            ttk.Label(left, text="Das Plugin gibt's als eigenen Download – ganz optional.",
                      style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        h(left, "Chat")
        self.chat_var = tk.BooleanVar()
        ttk.Checkbutton(left, text="Chat rechts im Fenster anzeigen", variable=self.chat_var,
                        command=self.on_chat_opt).pack(anchor="w")
        self.emote_var = tk.BooleanVar()
        ttk.Checkbutton(left, text="Emotes als Bild anzeigen (Twitch, BTTV, FFZ, 7TV)", variable=self.emote_var,
                        command=lambda: self.set_opt("emotes", self.emote_var.get())).pack(anchor="w", pady=(6, 0))

        # Twitch
        h(right, "Twitch-Konto", True)
        acc = ttk.Frame(right)
        acc.pack(fill="x")
        self.set_acc_lbl = ttk.Label(acc, text="")
        self.set_acc_lbl.pack(side="left")
        self.set_acc_btn = ttk.Button(acc, text="")
        self.set_acc_btn.pack(side="right")
        nf = ttk.Frame(right)
        nf.pack(fill="x", pady=(12, 0))
        ttk.Label(nf, text="Melden, wenn jemand live geht").pack(side="left")
        self.notify_opts = {"all": "Alle gefolgten Kanäle", "favs": "Nur Favoriten (★)", "off": "Aus"}
        self.notify_var = tk.StringVar()
        cb = ttk.Combobox(nf, textvariable=self.notify_var, values=list(self.notify_opts.values()),
                          state="readonly", width=22)
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self.set_opt(
            "notify", {v: k for k, v in self.notify_opts.items()}.get(self.notify_var.get(), "all")))

        h(right, "Kanal hinzufügen")
        ttk.Label(right, text="Erscheint in der Live-Liste, auch wenn du dem Kanal nicht folgst.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(right)
        row.pack(fill="x")
        e = ttk.Entry(row, font=(FONT, 10))
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Hinzufügen", style="Accent.TButton",
                   command=lambda: self.add_channel(e.get())).pack(side="left", padx=(6, 0))
        e.bind("<Return>", lambda ev: self.add_channel(e.get()))
        status = ttk.Label(right, text="", style="Stats.TLabel")
        status.pack(anchor="w", pady=(6, 0))

        lists = ttk.Frame(right)
        lists.pack(fill="both", expand=True, pady=(14, 0))
        lists.columnconfigure(0, weight=1, uniform="k")
        lists.columnconfigure(1, weight=1, uniform="k")

        def mk(col, title, btn_text, cmd):
            fr = ttk.Frame(lists)
            fr.grid(row=0, column=col, sticky="nsew", padx=(0, 10) if col == 0 else (10, 0))
            ttk.Label(fr, text=title, style="H.TLabel").pack(anchor="w", pady=(0, 6))
            t = ttk.Treeview(fr, columns=("name",), show="", height=7, selectmode="browse")
            t.pack(fill="x")
            ttk.Button(fr, text=btn_text, command=lambda: cmd(t)).pack(anchor="w", pady=(6, 0))
            return t

        extra_t = mk(0, "Zusätzliche Kanäle", "Entfernen", self.remove_extra)
        hidden_t = mk(1, "Ausgeblendete Kanäle", "Wieder anzeigen", self.unhide)
        self.manage_ui = {"entry": e, "status": status, "extra": extra_t, "hidden": hidden_t}

    def toggle_settings(self):
        if self.set_page.winfo_manager():
            self.close_settings()
        else:
            self.open_settings()

    def open_settings(self):
        st = self.settings
        self.theme_var.set(st.get("theme", "dark"))
        for k, e in self.hk_entries.items():
            e.delete(0, "end")
            e.insert(0, st.get(k, ""))
        self.snd_var.set(st.get("sound", True))
        self.chat_var.set(st.get("chat", True))
        self.emote_var.set(st.get("emotes", True))
        self.sd_var.set(st.get("streamdeck", True))
        self.sd_status_lbl.configure(text=self.sd_state)
        self.notify_var.set(self.notify_opts.get(st.get("notify", "all")))
        self.set_err.configure(text="")
        self.refresh_manage()
        self.update_account_ui()
        self.body_fr.pack_forget()
        self.set_page.pack(fill="both", expand=True)
        self.set_btn.configure(text="Schließen")

    def close_settings(self):
        new = {}
        for k, e in self.hk_entries.items():
            v = e.get().strip().lower()
            if v and pkb is not None:
                try:
                    pkb.HotKey.parse(v)
                except Exception:
                    self.set_err.configure(text=f"Hotkey „{v}“ versteh ich nicht – z. B. <ctrl>+<alt>+c",
                                           foreground=self.pal["danger"])
                    e.focus_set()
                    return
            new[k] = v
        if any(self.settings.get(k) != v for k, v in new.items()):
            self.settings.update(new)
            self.save()
            self.start_hotkeys()
            self.update_hk_label()
        self.set_page.pack_forget()
        self.body_fr.pack(fill="both", expand=True)
        self.set_btn.configure(text="Einstellungen")

    # ------------------------------------------------------------ Verknüpfung
    def build_shortcut_banner(self, outer):
        p = self.pal
        b = ttk.Frame(outer)
        self.banner = b
        ttk.Label(b, text="Desktop-Verknüpfung anlegen?", style="H.TLabel").pack(side="left")
        ttk.Label(b, text="Dann startest du den Stream-Merkzettel direkt vom Desktop. "
                          "Lass die exe am besten da liegen, wo sie jetzt ist.",
                  style="Stats.TLabel").pack(side="left", padx=14)
        ttk.Button(b, text="Nein, danke", command=lambda: self.answer_shortcut(False)).pack(side="right")
        ttk.Button(b, text="Ja, anlegen", style="Accent.TButton",
                   command=lambda: self.answer_shortcut(True)).pack(side="right", padx=6)

    def answer_shortcut(self, yes):
        self.settings["shortcut_asked"] = True
        self.save()
        self.banner.pack_forget()
        if yes:
            self.make_shortcut()

    def make_shortcut(self):
        if not is_exe():
            msg = "Geht nur in der fertigen .exe."
            self.sc_status.configure(text=msg)
            self.flash(msg)
            return
        self.sc_status.configure(text="Wird angelegt …")

        def worker():
            try:
                create_desktop_shortcut()
                self.events.put(("shortcut", True, "Verknüpfung liegt auf dem Desktop"))
            except Exception as e:
                self.events.put(("shortcut", False, f"Hat nicht geklappt: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def set_opt(self, key, value):
        self.settings[key] = value
        self.save()

    def on_theme_change(self):
        self.settings["theme"] = self.theme_var.get()
        self.save()
        self.apply_theme()

    def on_chat_opt(self):
        if bool(self.chat_var.get()) != bool(self.settings.get("chat", True)):
            self.toggle_chat()

    # ------------------------------------------------------------ Export/Import
    def to_clipboard(self, text, msg):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.flash(msg)

    def copy_text(self):
        s = self.need_stream()
        if not s:
            return
        out = [s["name"]]
        if s.get("url"):
            out.append(s["url"])
        out.append("\nClippen:")
        clips = sorted(s["clips"], key=lambda x: x["t"])
        out += [("[x] " if c.get("done") else "[ ] ") + fmt(c["t"]) + (f"  {c['note']}" if c.get("note") else "")
                for c in clips] or ["–"]
        out.append("\nNicht geschaut:")
        gaps = sorted(s["gaps"], key=lambda x: x["from"])
        out += [("[x] " if g.get("done") else "[ ] ") + f"{fmt(g['from'])} – {fmt(g['to'])}"
                + (f"  {g['note']}" if g.get("note") else "") for g in gaps] or ["–"]
        self.to_clipboard("\n".join(out), "Liste kopiert")

    def export_json(self):
        self.to_clipboard(json.dumps(self.state, ensure_ascii=False), "Backup in der Zwischenablage")

    def import_dialog(self):
        d, f = self.new_dialog("Backup einfügen")
        d.resizable(True, True)
        p = self.pal
        ttk.Label(f, text="Backup-Text einfügen (auch aus der Browser-Version):").pack(anchor="w")
        txt = tk.Text(f, width=70, height=12, wrap="word", bg=p["field"], fg=p["fg"],
                      insertbackground=p["fg"], relief="flat", padx=8, pady=8, font=(FONT, 9))
        txt.pack(fill="both", expand=True, pady=8)
        try:
            clip = self.root.clipboard_get()
            if clip.strip().startswith("{"):
                txt.insert("1.0", clip)
        except tk.TclError:
            pass
        ttk.Label(f, text="Streams mit gleicher ID werden überschrieben, andere bleiben.",
                  style="Muted.TLabel").pack(anchor="w")

        def ok():
            try:
                data = json.loads(txt.get("1.0", "end"))
                assert isinstance(data.get("streams"), list)
            except Exception:
                messagebox.showwarning("Backup", "Das ist kein gültiges Backup.", parent=d)
                return
            ids = {s["id"]: i for i, s in enumerate(self.state["streams"])}
            for ns in data["streams"]:
                ns.setdefault("clips", [])
                ns.setdefault("gaps", [])
                ns.setdefault("clock", {"base": 0, "startedAt": None})
                ns.setdefault("openGap", None)
                if ns["id"] in ids:
                    self.state["streams"][ids[ns["id"]]] = ns
                else:
                    self.state["streams"].append(ns)
            if not self.cur() and self.state["streams"]:
                self.state["current"] = self.state["streams"][0]["id"]
            self.save()
            self.render()
            self.flash(f"{len(data['streams'])} Stream(s) eingefügt")
            d.destroy()

        b = ttk.Frame(f)
        b.pack(anchor="e", pady=(12, 0))
        ttk.Button(b, text="Abbrechen", command=d.destroy).pack(side="left", padx=6)
        ttk.Button(b, text="Einfügen", style="Accent.TButton", command=ok).pack(side="left")
        self.center(d)

    def open_folder(self):
        folder = data_dir()
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception:
            messagebox.showinfo("Datenordner", folder)

    def on_close(self):
        self.save()
        if self.sd_srv:
            try:
                self.sd_srv.server_close()
            except Exception:
                pass
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
        self.root.destroy()


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
