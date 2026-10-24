"""
Раздача готовых серий на телефон (приложение AnimeDub для Android).

Только стандартная библиотека: модуль работает внутри AnimeDub.exe.
Каждый запрос должен нести ключ — в заголовке X-Key или параметре ?k=.
"""
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
DUB_RE = re.compile(r"^(?P<title>.+)_dub_(?P<lang>[a-z]{2})\.(?P<ext>mkv|mp4)$", re.IGNORECASE)
MIME = {"mkv": "video/x-matroska", "mp4": "video/mp4"}
CHUNK = 1 << 20
KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # без похожих символов: l/1, o/0


def new_key():
    k = "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))
    return f"{k[:4]}-{k[4:]}"


def file_id(path):
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def scan(folders):
    """Все готовые дубляжи (*_dub_xx.mkv/mp4) в папках библиотеки."""
    items = {}
    for folder in folders:
        d = Path(folder)
        if not d.is_dir():
            continue
        for f in d.iterdir():
            m = DUB_RE.match(f.name)
            if not m or not f.is_file():
                continue
            st = f.stat()
            items[file_id(f)] = {
                "id": file_id(f), "path": f, "title": m["title"], "lang": m["lang"].lower(),
                "folder": d.name, "size": st.st_size, "mtime": int(st.st_mtime),
                "mime": MIME[m["ext"].lower()]}
    return items


def addresses():
    """IPv4-адреса компьютера: домашняя сеть и Tailscale (100.64.0.0/10)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # пакет не уходит, только выбирается интерфейс
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    tailnet = ipaddress.ip_network("100.64.0.0/10")
    out = []
    for ip in sorted(ips):
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_link_local:
            continue
        out.append((ip, "Tailscale" if a in tailnet else "домашняя сеть"))
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "AnimeDub"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # в оконном exe нет консоли (sys.stderr = None)

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self, query):
        given = self.headers.get("X-Key") or query.get("k", [""])[0]
        return hmac.compare_digest(given.strip().lower().encode(), self.server.key.encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self._authorized(query):
            return self._json(401, {"error": "Неверный ключ"})
        if url.path == "/api/info":
            return self._json(200, {"app": "AnimeDub", "name": socket.gethostname(), "version": 1})
        if url.path == "/api/list":
            items = sorted(scan(self.server.folders()).values(),
                           key=lambda i: (i["folder"].lower(), i["title"].lower()))
            return self._json(200, [{k: v for k, v in i.items() if k != "path"} for i in items])
        if url.path.startswith("/video/"):
            item = scan(self.server.folders()).get(url.path[len("/video/"):])
            if not item:
                return self._json(404, {"error": "Серия не найдена"})
            return self._send_file(item)
        return self._json(404, {"error": "Нет такого адреса"})

    def _send_file(self, item):
        size = item["size"]
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m[1] or m[2]):
            if m[1]:
                start = int(m[1])
                end = min(int(m[2]), size - 1) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", item["mime"])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(item["path"], "rb") as f:
                f.seek(start)
                while length > 0:
                    buf = f.read(min(CHUNK, length))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    length -= len(buf)
        except (ConnectionError, OSError):
            pass  # телефон перемотал или закрыл плеер


class PhoneServer:
    """Сервер в фоновом потоке. folders — функция, возвращающая папки библиотеки."""

    def __init__(self, key, folders, port=PORT):
        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.key = key.lower()
        self.httpd.folders = folders
        self.port = port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
