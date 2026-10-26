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
import os
import socket
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
DUB_RE = re.compile(r"^(?P<title>.+)_dub_(?P<lang>[a-z]{2})\.(?P<ext>mkv|mp4)$", re.IGNORECASE)
MIME = {"mkv": "video/x-matroska", "mp4": "video/mp4"}
CHUNK = 1 << 20
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
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


def natural_key(text):
    """«Серия 2» раньше «Серия 10»."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


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
                           key=lambda i: (i["folder"].lower(), natural_key(i["title"])))
            return self._json(200, [{k: v for k, v in i.items() if k != "path"} for i in items])
        if url.path.startswith("/thumb/"):
            item = scan(self.server.folders()).get(url.path[len("/thumb/"):])
            data = self.server.thumb(item) if item else None
            if not data:
                return self._json(404, {"error": "Нет кадра"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=604800")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        if url.path.startswith("/video/"):
            item = scan(self.server.folders()).get(url.path[len("/video/"):])
            if not item:
                return self._json(404, {"error": "Серия не найдена"})
            return self._send_file(item)
        jobs = self.server.jobs
        if url.path in ("/api/sources", "/api/jobs") and not jobs:
            return self._json(503, {"error": "Озвучка с телефона недоступна"})
        if url.path == "/api/sources":
            return self._json(200, jobs.source_list())
        if url.path == "/api/jobs":
            return self._json(200, jobs.list())
        return self._json(404, {"error": "Нет такого адреса"})

    # ---------- озвучка с телефона ----------
    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ValueError("Слишком большой запрос")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if not self._authorized(urllib.parse.parse_qs(url.query)):
            self.close_connection = True
            return self._json(401, {"error": "Неверный ключ"})
        jobs = self.server.jobs
        if not jobs:
            self.close_connection = True
            return self._json(503, {"error": "Озвучка с телефона недоступна"})
        try:
            data = self._body_json()
        except ValueError as e:
            self.close_connection = True
            return self._json(400, {"error": str(e)})
        if url.path == "/api/dub":
            try:
                job = jobs.start(str(data.get("id", "")), str(data.get("lang") or "ru")[:5],
                                 str(data.get("context") or "")[:4000])
            except KeyError as e:
                return self._json(404, {"error": e.args[0]})
            return self._json(200, job)
        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", url.path)
        if m:
            return self._json(200, {"cancelled": jobs.cancel(m[1])})
        return self._json(404, {"error": "Нет такого адреса"})

    def do_PUT(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        self.close_connection = True  # тело большое — после ответа соединение не переиспользуем
        if not self._authorized(query):
            return self._json(401, {"error": "Неверный ключ"})
        jobs = self.server.jobs
        if url.path != "/api/upload" or not jobs:
            return self._json(404, {"error": "Нет такого адреса"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(411, {"error": "Нужен размер файла"})
        q = {k: v[0] for k, v in query.items()}
        try:
            source = jobs.upload(q.get("folder", ""), q.get("name", ""), self.rfile, length)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except (ConnectionError, OSError) as e:
            return self._json(500, {"error": f"Не удалось сохранить видео: {e}"})
        result = {"id": source}
        if q.get("dub") == "1":
            try:
                result["job"] = jobs.start(source, q.get("lang", "ru")[:5], q.get("context", ""))
            except KeyError as e:
                result["error"] = e.args[0]
        return self._json(200, result)

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


class Thumbs:
    """Кадр из серии (на 30% длительности) для обложек в приложении, с кэшем на диске."""

    def __init__(self, cache_dir, ffmpeg_dir):
        self.cache = Path(cache_dir)
        self.ffmpeg_dir = ffmpeg_dir  # функция: папка с ffmpeg.exe/ffprobe.exe или None
        self.sem = threading.Semaphore(2)
        self.lock = threading.Lock()
        self.busy = {}

    def _tool(self, name):
        d = self.ffmpeg_dir()
        exe = name + (".exe" if os.name == "nt" else "")
        return str(Path(d) / exe) if d else None

    def __call__(self, item):
        out = self.cache / f"{item['id']}_{item['mtime']}.jpg"
        if out.exists():
            return out.read_bytes()
        with self.lock:
            ev = self.busy.setdefault(out.name, threading.Lock())
        with ev, self.sem:  # один и тот же кадр не делаем дважды
            if out.exists():
                return out.read_bytes()
            ffmpeg, ffprobe = self._tool("ffmpeg"), self._tool("ffprobe")
            if not ffmpeg:
                return None
            self.cache.mkdir(parents=True, exist_ok=True)
            at = 90.0
            try:
                r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(item["path"])], capture_output=True,
                                   text=True, timeout=30, creationflags=NO_WINDOW)
                at = float(r.stdout.strip()) * 0.3
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            try:
                subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{at:.1f}",
                                "-i", str(item["path"]), "-frames:v", "1",
                                "-vf", "scale=640:-2", "-q:v", "5", str(out)],
                               capture_output=True, timeout=60, creationflags=NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                return None
            return out.read_bytes() if out.exists() else None


class PhoneServer:
    """Сервер в фоновом потоке. folders — функция, возвращающая папки библиотеки."""

    def __init__(self, key, folders, port=PORT, thumbs=None, jobs=None):
        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.key = key.lower()
        self.httpd.folders = folders
        self.httpd.thumb = thumbs or (lambda item: None)
        self.httpd.jobs = jobs
        self.port = port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
