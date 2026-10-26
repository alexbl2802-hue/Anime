"""
Очередь озвучки, запускаемой с телефона: серии выполняются по одной, на видеокарте ноутбука.

Только стандартная библиотека: модуль работает внутри AnimeDub.exe.
"""
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from server import DUB_RE, file_id, natural_key

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".webm"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# доля этапа в общем проценте (≈ время этапа)
WEIGHTS = {"extract": 2, "separate": 15, "transcribe": 20, "translate": 25, "synth": 30, "mix": 8}
ORDER = list(WEIGHTS)
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name, fallback):
    name = BAD_NAME.sub("_", (name or "").strip()).strip(" .")
    return name[:120] or fallback


class Jobs:
    """
    folders()        — папки библиотеки;
    make_cmd(v, l)   — команда конвейера dub.py для видео v и языка l (или исключение с причиной);
    env()            — окружение для дочернего процесса;
    gui_busy()       — занята ли видеокарта задачей из окна программы;
    upload_root      — куда класть видео, загруженные с телефона;
    on_folder(d)     — добавить новую папку в библиотеку;
    notify(text)     — строка в журнал окна программы.
    """

    def __init__(self, folders, make_cmd, env, gui_busy, upload_root, on_folder, notify):
        self.folders, self.make_cmd, self.env = folders, make_cmd, env
        self.gui_busy, self.on_folder, self.notify = gui_busy, on_folder, notify
        self.upload_root = Path(upload_root)
        self.jobs = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.proc = None
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------- исходные серии ----------
    def sources(self):
        """Видео в папках библиотеки, кроме уже готовых дубляжей."""
        out = {}
        for folder in self.folders():
            d = Path(folder)
            if not d.is_dir():
                continue
            names = {f.name for f in d.iterdir()}
            for f in d.iterdir():
                if f.suffix.lower() not in VIDEO_EXT or DUB_RE.match(f.name) or not f.is_file():
                    continue
                dubbed = sorted(m["lang"].lower() for n in names
                                if (m := DUB_RE.match(n)) and m["title"] == f.stem)
                out[file_id(f)] = {"id": file_id(f), "path": f, "title": f.stem,
                                   "folder": d.name, "size": f.stat().st_size, "dubbed": dubbed}
        return out

    def source_list(self):
        items = sorted(self.sources().values(),
                       key=lambda i: (i["folder"].lower(), natural_key(i["title"])))
        return [{k: v for k, v in i.items() if k != "path"} for i in items]

    # ---------- задания ----------
    def _public(self, j):
        return {k: v for k, v in j.items() if k not in ("path", "ctx")}

    def list(self):
        with self.lock:
            return [self._public(j) for j in self.jobs[-30:]][::-1]

    def busy(self):
        with self.lock:
            return any(j["state"] == "running" for j in self.jobs)

    def start(self, source_id, lang, ctx=""):
        src = self.sources().get(source_id)
        if not src:
            raise KeyError("Серия не найдена на компьютере")
        with self.lock:
            for j in self.jobs:
                if j["source"] == source_id and j["lang"] == lang and j["state"] in ("queued", "running"):
                    return self._public(j)
            job = {"id": secrets.token_hex(4), "source": source_id, "path": src["path"],
                   "title": src["title"], "folder": src["folder"], "lang": lang,
                   "ctx": (ctx or "").strip(), "state": "queued", "stage": "", "percent": 0,
                   "message": "В очереди", "created": int(time.time())}
            self.jobs.append(job)
        self.notify(f"📱 С телефона: в очередь на озвучку «{job['title']}»")
        self.wake.set()
        return self._public(job)

    def cancel(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if not job or job["state"] not in ("queued", "running"):
                return False
            was_running = job["state"] == "running"
            job.update(state="cancelled", message="Отменено")
            proc = self.proc if was_running else None
        if proc:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                proc.kill()
        return True

    def _worker(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    job = next((j for j in self.jobs if j["state"] == "queued"), None)
                if not job:
                    break
                while self.gui_busy():  # окно программы занимает видеокарту — ждём
                    with self.lock:
                        job["message"] = "Ждёт: на компьютере идёт другая задача"
                    time.sleep(3)
                    if job["state"] != "queued":
                        break
                if job["state"] == "queued":
                    self._run(job)

    def _run(self, job):
        video = Path(job["path"])
        if job["ctx"]:
            work = video.parent / f"work_{video.stem}"
            work.mkdir(exist_ok=True)
            (work / "context.txt").write_text(job["ctx"], encoding="utf-8")
        try:
            cmd = self.make_cmd(str(video), job["lang"])
        except Exception as e:  # noqa: BLE001 — причину показываем на телефоне
            job.update(state="failed", message=str(e))
            return
        with self.lock:
            if job["state"] != "queued":
                return
            job.update(state="running", message="Запуск", percent=0)
        self.notify(f"📱 С телефона: озвучка «{job['title']}» началась")
        tail = deque(maxlen=40)
        try:
            self.proc = subprocess.Popen(
                [str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=self.env(),
                creationflags=NO_WINDOW)
            for line in self.proc.stdout:
                self._progress(job, line.rstrip(), tail)
            rc = self.proc.wait()
        except OSError as e:
            rc, tail = -1, deque([str(e)])
        finally:
            self.proc = None
        with self.lock:
            if job["state"] == "cancelled":
                pass
            elif rc == 0:
                job.update(state="done", percent=100, stage="mix", message="Готово")
            else:
                err = next((l for l in reversed(tail) if l.strip()), f"код {rc}")
                job.update(state="failed", message=err[:300])
        self.notify(f"📱 С телефона: «{job['title']}» — {job['message']}")

    def _progress(self, job, s, tail):
        if s.startswith("@@PROGRESS"):
            try:
                _, stage, cur, total = s.split()
                frac = int(cur) / max(int(total), 1)
            except ValueError:
                return
            job["stage"] = stage
        elif s.startswith("=== ") and s.endswith(" ==="):
            stage, frac = s.strip("= ").strip(), 0.0
            job["stage"] = stage
        else:
            if s:
                tail.append(s)
            return
        if stage in WEIGHTS:
            before = sum(WEIGHTS[x] for x in ORDER[:ORDER.index(stage)])
            pct = (before + WEIGHTS[stage] * min(frac, 1)) * 100 / sum(WEIGHTS.values())
            job["percent"] = max(job["percent"], int(pct))
            job["message"] = ""

    # ---------- загрузка с телефона ----------
    def upload(self, folder, name, stream, length):
        """Сохраняет видео с телефона в upload_root/<аниме>/<файл>; возвращает id серии."""
        ext = Path(name or "").suffix.lower()
        if ext not in VIDEO_EXT:
            raise ValueError("Это не видео (нужен mkv, mp4, avi, mov или webm)")
        d = self.upload_root / safe_name(folder, "С телефона")
        d.mkdir(parents=True, exist_ok=True)
        dest = d / (safe_name(Path(name).stem, "Серия") + ext)
        part = dest.with_name(dest.name + ".part")
        left = length
        with open(part, "wb") as f:
            while left > 0:
                buf = stream.read(min(1 << 20, left))
                if not buf:
                    break
                f.write(buf)
                left -= len(buf)
        if left > 0:
            part.unlink(missing_ok=True)
            raise ConnectionError("Загрузка оборвалась")
        part.replace(dest)
        self.on_folder(d)
        self.notify(f"📱 С телефона загружено видео «{dest.name}» в {d}")
        return file_id(dest)
