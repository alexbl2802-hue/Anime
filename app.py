"""
AnimeDub — оконная программа для дубляжа аниме голосами персонажей.

Сам .exe лёгкий: при первом запуске он ставит Python-окружение с нейросетями
в %LOCALAPPDATA%\\AnimeDub и дальше запускает конвейер dub.py оттуда.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from server import PORT, PhoneServer, addresses, new_key

APP_NAME = "AnimeDub"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
VENV = APP_DIR / "venv"
VPY = VENV / "Scripts" / "python.exe"
CONFIG = APP_DIR / "config.json"
TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
TORCH_PINS = ["torch==2.5.1", "torchaudio==2.5.1"]  # новее ломают demucs (torch.load) и torchaudio.load
REQS = ["coqui-tts", "faster-whisper", "demucs", "anthropic", "soundfile", "librosa"]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CHARS_PER_SEC = 14

LANGS = [("Русский", "ru"), ("English", "en"), ("Українська", "uk"), ("Español", "es"),
         ("Deutsch", "de"), ("Français", "fr"), ("Italiano", "it"), ("Português", "pt"),
         ("Polski", "pl"), ("Türkçe", "tr")]
STAGE_RU = {"extract": "Извлечение звука", "separate": "Отделение голоса от музыки",
            "transcribe": "Распознавание речи", "translate": "Перевод",
            "synth": "Озвучка", "mix": "Сведение"}
GPU_CHECK = ("import torch;ok=torch.cuda.is_available();print('Видеокарта:', "
             "torch.cuda.get_device_name(0) if ok else 'не найдена — работа пойдёт на процессоре (медленно)')")
TQDM_RE = re.compile(r"\d+%\|")


class StepFailed(Exception):
    pass


# ---------- окружение ----------
def resource(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / name


def dub_script():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    dst = APP_DIR / "dub.py"
    shutil.copyfile(resource("dub.py"), dst)
    return dst


def child_env(api_key=""):
    env = os.environ.copy()
    local = os.environ.get("LOCALAPPDATA", "")
    extra = [str(Path(local) / "Microsoft" / "WinGet" / "Links"),
             str(Path(local) / "Programs" / "Python" / "Launcher")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               COQUI_TOS_AGREED="1")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def find_python():
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    cands = [["py", "-3.11"], ["py", "-3.12"], ["py", "-3.10"],
             [str(local / "Programs" / "Python" / "Python311" / "python.exe")],
             ["python"]]
    for c in cands:
        try:
            r = subprocess.run(c + ["-c", "import sys;print(sys.version_info[1])"],
                               capture_output=True, text=True, timeout=30,
                               creationflags=NO_WINDOW, env=child_env())
            if r.returncode == 0 and r.stdout.strip() in ("10", "11", "12"):
                return c
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def load_cfg():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def work_dir(video):
    v = Path(video)
    return v.parent / f"work_{v.stem}"


# ---------- главное окно ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AnimeDub — дубляж аниме голосами персонажей")
        self.geometry("860x760")
        self.minsize(720, 600)
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.proc = None
        self.stopping = False
        self.phone = None
        self._build()
        if self.phone_on.get():
            self._phone_toggle()
        self.after(100, self._poll)
        if not self.cfg.get("installed"):
            self.log("Первый запуск: нажми «Установить компоненты».\n"
                     "Скачается около 10 ГБ, это займёт 20–60 минут.\n")

    # --- интерфейс ---
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Видео:").grid(row=0, column=0, sticky="w", **pad)
        self.video = tk.StringVar(value=self.cfg.get("last_video", ""))
        ttk.Entry(top, textvariable=self.video).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(top, text="Выбрать…", command=self.pick_video).grid(row=0, column=2, **pad)

        ttk.Label(top, text="Язык дубляжа:").grid(row=1, column=0, sticky="w", **pad)
        row = ttk.Frame(top)
        row.grid(row=1, column=1, columnspan=2, sticky="w")
        self.lang = tk.StringVar(value=self.cfg.get("lang_name", "Русский"))
        ttk.Combobox(row, textvariable=self.lang, values=[n for n, _ in LANGS],
                     state="readonly", width=16).pack(side="left", padx=6)
        self.honor = tk.BooleanVar(value=self.cfg.get("honorifics", False))
        ttk.Checkbutton(row, text="Оставлять -сан, -кун, -тян",
                        variable=self.honor).pack(side="left", padx=12)

        ttk.Label(top, text="Ключ Claude API:").grid(row=2, column=0, sticky="w", **pad)
        self.key = tk.StringVar(value=self.cfg.get("api_key", ""))
        ttk.Entry(top, textvariable=self.key, show="•").grid(row=2, column=1, columnspan=2,
                                                             sticky="ew", **pad)

        ttk.Label(top, text="Персонажи и манера\nречи (для перевода):").grid(
            row=3, column=0, sticky="nw", **pad)
        self.ctx = tk.Text(top, height=5, wrap="word", font=("Segoe UI", 10))
        self.ctx.grid(row=3, column=1, columnspan=2, sticky="ew", **pad)

        gains = ttk.Frame(top)
        gains.grid(row=4, column=1, columnspan=2, sticky="w")
        ttk.Label(top, text="Громкость:").grid(row=4, column=0, sticky="w", **pad)
        self.vgain = tk.DoubleVar(value=self.cfg.get("voice_gain", 1.0))
        self.bgain = tk.DoubleVar(value=self.cfg.get("bg_gain", 1.0))
        ttk.Label(gains, text="голос").pack(side="left", padx=4)
        ttk.Scale(gains, from_=0.3, to=2.0, variable=self.vgain, length=150).pack(side="left")
        ttk.Label(gains, text="   музыка/фон").pack(side="left", padx=4)
        ttk.Scale(gains, from_=0.3, to=2.0, variable=self.bgain, length=150).pack(side="left")

        ttk.Label(top, text="Телефон:").grid(row=5, column=0, sticky="nw", **pad)
        phone = ttk.Frame(top)
        phone.grid(row=5, column=1, columnspan=2, sticky="ew")
        self.phone_on = tk.BooleanVar(value=self.cfg.get("phone", False))
        ttk.Checkbutton(phone, text="Смотреть серии в приложении на Android",
                        variable=self.phone_on, command=self._phone_toggle).pack(anchor="w", padx=6)
        ttk.Button(phone, text="Добавить папку с сериями…",
                   command=self.add_library).pack(anchor="w", padx=6, pady=2)
        self.phone_info = tk.Text(phone, height=3, wrap="word", font=("Segoe UI", 9),
                                  relief="flat", bg=self.cget("bg"), state="disabled")
        self.phone_info.pack(fill="x", padx=6)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=4)
        self.b_setup = ttk.Button(btns, text="Установить компоненты", command=self.setup)
        self.b_tr = ttk.Button(btns, text="1. Распознать и перевести",
                               command=lambda: self.start_dub("translate"))
        self.b_ed = ttk.Button(btns, text="Редактор перевода", command=self.open_editor)
        self.b_dub = ttk.Button(btns, text="2. Озвучить и собрать видео",
                                command=lambda: self.start_dub(None))
        self.b_stop = ttk.Button(btns, text="Стоп", command=self.stop, state="disabled")
        self.b_open = ttk.Button(btns, text="Открыть папку", command=self.open_folder)
        for b in (self.b_setup, self.b_tr, self.b_ed, self.b_dub, self.b_stop, self.b_open):
            b.pack(side="left", padx=3)

        st = ttk.Frame(self)
        st.pack(fill="x", padx=8, pady=2)
        self.status = ttk.Label(st, text="Готов к работе")
        self.status.pack(anchor="w")
        self.bar = ttk.Progressbar(st, maximum=100)
        self.bar.pack(fill="x", pady=2)

        logf = ttk.Frame(self)
        logf.pack(fill="both", expand=True, padx=8, pady=6)
        self.logbox = tk.Text(logf, wrap="word", state="disabled", font=("Consolas", 9),
                              bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4")
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)

        if self.video.get():
            self._load_ctx()

    def log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text)
        if int(self.logbox.index("end-1c").split(".")[0]) > 4000:
            self.logbox.delete("1.0", "1000.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def set_status(self, text, pct=None):
        self.status.configure(text=text)
        if pct is not None:
            self.bar.configure(value=pct)

    def _set_busy(self, busy):
        for b in (self.b_setup, self.b_tr, self.b_ed, self.b_dub):
            b.configure(state="disabled" if busy else "normal")
        self.b_stop.configure(state="normal" if busy else "disabled")

    # --- выбор видео и контекста ---
    def pick_video(self):
        f = filedialog.askopenfilename(
            title="Выбери серию",
            filetypes=[("Видео", "*.mkv *.mp4 *.avi *.mov *.webm"), ("Все файлы", "*.*")])
        if f:
            self.video.set(f)
            self._load_ctx()

    def _ctx_file(self):
        return work_dir(self.video.get()) / "context.txt"

    def _load_ctx(self):
        self.ctx.delete("1.0", "end")
        p = self._ctx_file()
        if p.exists():
            self.ctx.insert("1.0", p.read_text(encoding="utf-8"))
        elif self.cfg.get("last_ctx"):
            self.ctx.insert("1.0", self.cfg["last_ctx"])  # та же серия тайтла — те же персонажи

    def _save_settings(self):
        ctx = self.ctx.get("1.0", "end").strip()
        self.cfg.update(api_key=self.key.get().strip(), lang_name=self.lang.get(),
                        honorifics=self.honor.get(), voice_gain=round(self.vgain.get(), 2),
                        bg_gain=round(self.bgain.get(), 2), last_video=self.video.get(),
                        last_ctx=ctx)
        save_cfg(self.cfg)
        return ctx

    # --- доступ с телефона ---
    def _library(self):
        return list(self.cfg.get("library", []))

    def _add_to_library(self, folder):
        folder = str(Path(folder))
        lib = self._library()
        if folder not in lib:
            self.cfg["library"] = lib + [folder]
            save_cfg(self.cfg)

    def add_library(self):
        d = filedialog.askdirectory(title="Папка с готовыми сериями (*_dub_*.mkv)")
        if d:
            self._add_to_library(d)
            self._phone_status()

    def _phone_status(self, text=None):
        if text is None:
            if not self.phone:
                text = "Выключено."
            else:
                addrs = addresses() or [("127.0.0.1", "только этот компьютер")]
                text = ("Адрес: " + ", ".join(f"{ip}:{PORT} ({kind})" for ip, kind in addrs)
                        + f"\nКлюч: {self.cfg['phone_key']}    Папок с сериями: {len(self._library())}")
        self.phone_info.configure(state="normal")
        self.phone_info.delete("1.0", "end")
        self.phone_info.insert("1.0", text)
        self.phone_info.configure(state="disabled")

    def _phone_toggle(self):
        if self.phone:
            self.phone.stop()
            self.phone = None
        if self.phone_on.get():
            key = self.cfg.setdefault("phone_key", new_key())
            try:
                self.phone = PhoneServer(key, self._library)
            except OSError as e:
                self.phone_on.set(False)
                messagebox.showerror(APP_NAME, f"Не удалось открыть порт {PORT}: {e}")
        self.cfg["phone"] = self.phone_on.get()
        save_cfg(self.cfg)
        self._phone_status()

    # --- запуск фоновых задач ---
    def _stream(self, title, cmd):
        if self.stopping:
            raise StepFailed("Остановлено")
        self.q.put(("log", f"\n▶ {title}\n"))
        try:
            self.proc = subprocess.Popen(
                [str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=child_env(self.cfg.get("api_key", "")), creationflags=NO_WINDOW)
        except FileNotFoundError:
            raise StepFailed(f"Не найдена программа «{cmd[0]}».")
        for line in self.proc.stdout:
            self.q.put(("line", line))
        rc = self.proc.wait()
        self.proc = None
        if self.stopping:
            raise StepFailed("Остановлено")
        if rc != 0:
            raise StepFailed(f"Этап «{title}» завершился с ошибкой (код {rc}). Подробности в журнале.")

    def run_task(self, fn, on_success=None):
        self.stopping = False
        self._set_busy(True)

        def worker():
            try:
                fn()
                self.q.put(("done", on_success))
            except StepFailed as e:
                self.q.put(("fail", str(e)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fail", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.stopping = True
        if self.proc:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "line":
                    self._handle_line(data)
                elif kind == "log":
                    self.log(data)
                elif kind == "done":
                    self._set_busy(False)
                    self.set_status("Готово", 100)
                    if data:
                        data()
                elif kind == "fail":
                    self._set_busy(False)
                    self.set_status("Ошибка" if data != "Остановлено" else "Остановлено")
                    self.log(f"\n✖ {data}\n")
                    if data != "Остановлено":
                        messagebox.showerror(APP_NAME, data)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle_line(self, line):
        s = line.rstrip()
        if s.startswith("@@PROGRESS"):
            _, stage, cur, total = s.split()
            total = max(int(total), 1)
            self.set_status(f"{STAGE_RU.get(stage, stage)}: {cur} из {total}",
                            int(cur) * 100 / total)
        elif s.startswith("=== ") and s.endswith(" ==="):
            stage = s.strip("= ").strip()
            self.set_status(STAGE_RU.get(stage, stage), 0)
            self.log(f"\n— {STAGE_RU.get(stage, stage)} —\n")
        elif TQDM_RE.search(s):
            m = re.search(r"(\d+)%\|", s)
            self.set_status(self.status.cget("text").split(" (")[0] + f" ({m.group(1)}%)",
                            int(m.group(1)))
        elif s:
            self.log(s + "\n")

    # --- установка ---
    def setup(self):
        if not messagebox.askyesno(
                APP_NAME, "Будет скачано около 10 ГБ (Python, PyTorch, нейросети).\n"
                          "Это займёт 20–60 минут. Продолжить?"):
            return
        self.run_task(self._setup, lambda: messagebox.showinfo(
            APP_NAME, "Всё установлено! Можно выбирать серию."))

    def _setup(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        winget = ["winget", "install", "-e", "--silent",
                  "--accept-package-agreements", "--accept-source-agreements", "--id"]
        py = find_python()
        if not py:
            self._stream("Установка Python 3.11", winget + ["Python.Python.3.11"])
            py = find_python()
            if not py:
                raise StepFailed("Python установлен, но пока не виден. Перезапусти AnimeDub "
                                 "и снова нажми «Установить компоненты».")
        if not shutil.which("ffmpeg", path=child_env()["PATH"]):
            self._stream("Установка ffmpeg", winget + ["Gyan.FFmpeg"])
        if not VPY.exists():
            self._stream("Создание окружения", py + ["-m", "venv", str(VENV)])
        pip = [str(VPY), "-m", "pip", "install", "--disable-pip-version-check",
               "--progress-bar", "off"]
        self._stream("Обновление pip", pip + ["--upgrade", "pip"])
        self._stream("PyTorch с поддержкой видеокарты (~3 ГБ)",
                     pip + TORCH_PINS + ["--index-url", TORCH_INDEX])
        self._stream("Нейросети и библиотеки",
                     pip + REQS + TORCH_PINS + ["--extra-index-url", TORCH_INDEX])
        self._stream("Проверка видеокарты", [str(VPY), "-c", GPU_CHECK])
        self.cfg["installed"] = True
        save_cfg(self.cfg)

    # --- дубляж ---
    def _ready(self):
        if not self.cfg.get("installed") or not VPY.exists():
            messagebox.showwarning(APP_NAME, "Сначала нажми «Установить компоненты».")
            return False
        v = self.video.get()
        if not v or not Path(v).exists():
            messagebox.showwarning(APP_NAME, "Выбери файл серии.")
            return False
        return True

    def _needs_translation(self):
        p = work_dir(self.video.get()) / "lines.json"
        if not p.exists():
            return True
        try:
            return not all(l["tr"] or l.get("done") for l in json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return True

    def start_dub(self, until):
        if not self._ready():
            return
        ctx = self._save_settings()
        if self._needs_translation() and not self.cfg.get("api_key"):
            messagebox.showwarning(APP_NAME, "Для перевода нужен ключ Claude API "
                                             "(console.anthropic.com).")
            return
        video = self.video.get()
        wd = work_dir(video)
        wd.mkdir(exist_ok=True)
        self._add_to_library(Path(video).parent)
        self._ctx_file().write_text(ctx, encoding="utf-8")
        code = dict(LANGS)[self.lang.get()]
        cmd = [str(VPY), str(dub_script()), video, "--lang", code,
               "--context", str(self._ctx_file()),
               "--voice-gain", str(self.cfg["voice_gain"]),
               "--bg-gain", str(self.cfg["bg_gain"])]
        if self.honor.get():
            cmd.append("--keep-honorifics")
        if until:
            cmd += ["--until", until]
            title, done = "Распознавание и перевод", self.open_editor
        else:
            title, done = "Дубляж", self._finished
        self.run_task(lambda: self._stream(title, cmd), done)

    def _finished(self):
        v = Path(self.video.get())
        out = v.parent / f"{v.stem}_dub_{dict(LANGS)[self.lang.get()]}.mkv"
        if messagebox.askyesno(APP_NAME, f"Готово!\n{out.name}\n\nОткрыть папку?"):
            self.open_folder()

    def open_folder(self):
        v = self.video.get()
        if v and Path(v).parent.exists():
            os.startfile(Path(v).parent)  # noqa: S606

    def open_editor(self):
        if not self.video.get():
            messagebox.showwarning(APP_NAME, "Выбери файл серии.")
            return
        p = work_dir(self.video.get()) / "lines.json"
        if not p.exists():
            messagebox.showinfo(APP_NAME, "Сначала нажми «1. Распознать и перевести».")
            return
        Editor(self, p)


# ---------- редактор перевода ----------
class Editor(tk.Toplevel):
    def __init__(self, master, path):
        super().__init__(master)
        self.title("Редактор перевода")
        self.geometry("980x640")
        self.path = path
        self.lines = json.loads(path.read_text(encoding="utf-8"))
        self.by_id = {str(l["id"]): l for l in self.lines}
        self.cur = None
        self.dirty = False

        ttk.Label(self, text="Выбери реплику, исправь перевод и нажми «Применить» "
                             "(Ctrl+Enter). Изменённые реплики переозвучатся при следующем запуске.",
                  wraplength=940).pack(anchor="w", padx=8, pady=6)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(frame, columns=("t", "ja", "tr"), show="headings")
        for col, name, w in (("t", "Время", 70), ("ja", "Оригинал", 380), ("tr", "Перевод", 460)):
            self.tree.heading(col, text=name)
            self.tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("long", foreground="#c0392b")
        for l in self.lines:
            self.tree.insert("", "end", iid=str(l["id"]), values=self._row(l), tags=self._tags(l))
        self.tree.bind("<<TreeviewSelect>>", self._select)

        ed = ttk.Frame(self)
        ed.pack(fill="x", padx=8, pady=6)
        self.ja = ttk.Label(ed, text="", wraplength=940, font=("Segoe UI", 10, "bold"))
        self.ja.pack(anchor="w")
        self.tr = tk.Text(ed, height=3, wrap="word", font=("Segoe UI", 11))
        self.tr.pack(fill="x", pady=4)
        self.tr.bind("<KeyRelease>", lambda e: self._count())
        self.tr.bind("<Control-Return>", lambda e: (self.apply(), "break")[1])
        bottom = ttk.Frame(ed)
        bottom.pack(fill="x")
        self.cnt = ttk.Label(bottom, text="")
        self.cnt.pack(side="left")
        ttk.Button(bottom, text="Сохранить и закрыть", command=self.save_close).pack(side="right")
        ttk.Button(bottom, text="Применить", command=self.apply).pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.close)

    @staticmethod
    def _limit(l):
        return max(4, int((l["end"] - l["start"]) * CHARS_PER_SEC))

    def _row(self, l):
        m, s = divmod(int(l["start"]), 60)
        return (f"{m:02d}:{s:02d}", l["ja"], l["tr"])

    def _tags(self, l):
        return ("long",) if len(l["tr"]) > self._limit(l) * 1.25 else ()

    def _select(self, _):
        sel = self.tree.selection()
        if not sel:
            return
        self.cur = self.by_id[sel[0]]
        self.ja.configure(text=self.cur["ja"])
        self.tr.delete("1.0", "end")
        self.tr.insert("1.0", self.cur["tr"])
        self._count()

    def _count(self):
        if self.cur:
            n = len(self.tr.get("1.0", "end").strip())
            lim = self._limit(self.cur)
            self.cnt.configure(text=f"{n} / ~{lim} символов" + ("  — длинновато, будет ускорено"
                                                                if n > lim * 1.25 else ""))

    def apply(self):
        if not self.cur:
            return
        self.cur["tr"] = self.tr.get("1.0", "end").strip()
        iid = str(self.cur["id"])
        self.tree.item(iid, values=self._row(self.cur), tags=self._tags(self.cur))
        self.dirty = True
        nxt = self.tree.next(iid)
        if nxt:
            self.tree.selection_set(nxt)
            self.tree.see(nxt)

    def save(self):
        self.path.write_text(json.dumps(self.lines, ensure_ascii=False, indent=1), encoding="utf-8")
        self.dirty = False

    def save_close(self):
        self.save()
        self.destroy()

    def close(self):
        if self.dirty:
            ans = messagebox.askyesnocancel("Редактор перевода", "Сохранить изменения?", parent=self)
            if ans is None:
                return
            if ans:
                self.save()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
