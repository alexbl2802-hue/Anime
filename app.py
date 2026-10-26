"""
AnimeDub — оконная программа для дубляжа аниме голосами персонажей.

Сам .exe лёгкий: при первом запуске он ставит Python-окружение с нейросетями
в %LOCALAPPDATA%\\AnimeDub и дальше запускает конвейер dub.py оттуда.
"""
import collections
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from jobs import Jobs
from server import PORT, PhoneServer, Thumbs, addresses, new_key

APP_NAME = "AnimeDub"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
VENV = APP_DIR / "venv"
VPY = VENV / "Scripts" / "python.exe"
CONFIG = APP_DIR / "config.json"
UPLOADS = Path.home() / "Videos" / "AnimeDub"  # сюда кладутся видео, загруженные с телефона
PY_DIR = APP_DIR / "python311"
FFMPEG_DIR = APP_DIR / "ffmpeg"
OLLAMA_URLS = ["https://ollama.com/download/OllamaSetup.exe",
               "https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe"]
OLLAMA_API = "http://127.0.0.1:11434"
TRANSLATORS = [("Локально — бесплатно, без ключа", "local"),
               ("Claude — лучше качество, нужен ключ", "claude")]
# модель перевода под память видеокарты: (до ГБ, модель, размер)
LOCAL_MODELS = [(11, "qwen2.5:7b", "4.7 ГБ"), (20, "qwen2.5:14b", "9 ГБ"),
                (999, "qwen2.5:32b", "20 ГБ")]
VRAM_CHECK = ("import torch;print(torch.cuda.get_device_properties(0).total_memory / 2**30 "
              "if torch.cuda.is_available() else 0)")
PY_URLS = ["https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"]
FFMPEG_URLS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]
TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
# Зеркала на случай, если провайдер или антивирус режет pypi.org / download.pytorch.org.
# Перед установкой проверяется, какое отвечает, и берётся первое рабочее.
PYPI_MIRRORS = ["https://pypi.org/simple",
                "https://mirrors.aliyun.com/pypi/simple",
                "https://pypi.tuna.tsinghua.edu.cn/simple",
                "https://mirrors.cloud.tencent.com/pypi/simple"]
# ("index", url) — каталог в формате PyPI; ("links", url) — страница со списком файлов
TORCH_SOURCES = [("index", TORCH_INDEX),
                 ("links", "https://mirrors.aliyun.com/pytorch-wheels/cu124/"),
                 ("index", "https://mirrors.nju.edu.cn/pytorch/whl/cu124")]
TORCH_PINS = ["torch==2.5.1", "torchaudio==2.5.1"]  # новее ломают demucs (torch.load) и torchaudio.load
REQS = ["coqui-tts", "faster-whisper", "demucs==4.0.1", "anthropic", "soundfile", "librosa"]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CHARS_PER_SEC = 14

LANGS = [("Русский", "ru"), ("English", "en"), ("Українська", "uk"), ("Español", "es"),
         ("Deutsch", "de"), ("Français", "fr"), ("Italiano", "it"), ("Português", "pt"),
         ("Polski", "pl"), ("Türkçe", "tr")]
STAGE_RU = {"extract": "Извлечение звука", "separate": "Отделение голоса от музыки",
            "transcribe": "Распознавание речи", "translate": "Перевод",
            "synth": "Озвучка", "mix": "Сведение"}
GPU_CHECK = ("import torch;ok=torch.cuda.is_available();print('Видеокарта:', "
             "f'{torch.cuda.get_device_name(0)}, "
             "{torch.cuda.get_device_properties(0).total_memory / 2**30:.1f} ГБ' if ok "
             "else 'не найдена — работа пойдёт на процессоре (медленно)')")
PY_CHECK = "import sys,struct;print(sys.version_info[1], struct.calcsize('P') * 8)"
TQDM_RE = re.compile(r"\d+%\|")
PIP_PROGRESS_RE = re.compile(r"^Progress (\d+) of (\d+)$")
PIP_DOWNLOAD_RE = re.compile(r"^Downloading (\S+)")


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
    extra = [str(FFMPEG_DIR / "bin"), str(Path(local) / "Microsoft" / "WinGet" / "Links"),
             str(Path(local) / "Programs" / "Python" / "Launcher")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               COQUI_TOS_AGREED="1")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def find_python():
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    cands = [[str(PY_DIR / "python.exe")], ["py", "-3.11"], ["py", "-3.12"], ["py", "-3.10"],
             [str(local / "Programs" / "Python" / "Python311" / "python.exe")],
             ["python"]]
    for c in cands:
        if python_ok(c):
            return c
    return None


def python_ok(cmd):
    """PyTorch 2.5.1 есть только для 64-битного Python 3.10–3.12."""
    try:
        r = subprocess.run(cmd + ["-c", PY_CHECK], capture_output=True, text=True, timeout=30,
                           creationflags=NO_WINDOW, env=child_env())
        return r.returncode == 0 and r.stdout.split() in (["10", "64"], ["11", "64"], ["12", "64"])
    except (OSError, subprocess.TimeoutExpired):
        return False


def reachable(url, must_contain=""):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pip/24.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(4 << 20).decode("utf-8", "replace")
        return r.status == 200 and must_contain in body
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pick_sources():
    """Первое отвечающее зеркало PyPI и первый источник PyTorch 2.5.1."""
    pypi = next((u for u in PYPI_MIRRORS if reachable(u + "/pip/", "pip-")), None)
    torch = next(((kind, u) for kind, u in TORCH_SOURCES
                  if reachable(u.rstrip("/") + "/torch/" if kind == "index" else u,
                               "torch-2.5.1")), None)
    return pypi, torch


def explain_failure(tail):
    """Понятная причина ошибки pip/Python по последним строкам вывода."""
    text = "\n".join(tail)
    low = text.lower()
    rules = [
        (("no space left", "errno 28", "not enough space"),
         "На диске закончилось место. Для установки нужно около 15 ГБ свободного места на диске C:."),
        (("no matching distribution", "could not find a version"),
         "Не удалось найти пакет. Чаще всего не открывается сайт download.pytorch.org — проверь "
         "интернет, выключи VPN/прокси или разреши AnimeDub в антивирусе. Если не поможет, удали "
         "папку %LOCALAPPDATA%\\AnimeDub\\venv и снова нажми «Установить компоненты»."),
        (("10054", "connectionreseterror", "connection aborted"),
         "Сервер с пакетами разрывает соединение — так бывает, когда его блокирует провайдер "
         "или антивирус. Включи VPN (или выключи, если он уже включён) либо временно отключи "
         "проверку HTTPS в антивирусе и нажми «Установить компоненты» ещё раз."),
        (("readtimeout", "timed out", "connection reset", "connectionerror", "incompleteread",
          "remote end closed", "max retries exceeded", "connection broken"),
         "Оборвалось соединение при скачивании. Нажми «Установить компоненты» ещё раз — "
         "уже скачанное повторно качаться не будет."),
        (("winerror 5", "access is denied", "отказано в доступе", "permission denied"),
         "Нет доступа к файлу. Закрой другие копии AnimeDub; если не поможет — добавь папку "
         "%LOCALAPPDATA%\\AnimeDub в исключения антивируса."),
        (("winerror 126", "winerror 127", "fbgemm.dll", "c10.dll"),
         "Не хватает компонента Windows Microsoft Visual C++ Redistributable. Установи его с "
         "https://aka.ms/vs/17/release/vc_redist.x64.exe и снова нажми «Установить компоненты»."),
        (("hash", "do not match"),
         "Скачанный файл повреждён. Нажми «Установить компоненты» ещё раз."),
    ]
    for keys, msg in rules:
        if any(k in low for k in keys):
            return msg
    return ""


def ollama_exe():
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(p) if p.exists() else shutil.which("ollama")


def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA_API + "/api/version", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_API + "/api/tags", timeout=10) as r:
            return {m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])}
    except (urllib.error.URLError, OSError, ValueError):
        return set()


def mb(n):
    return f"{n / 2**30:.2f} ГБ" if n >= 2**30 else f"{n / 2**20:.0f} МБ"


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
        self.step = None  # (заголовок, начало, доля) текущего шага установки
        self.dl_name = ""
        self.phone = None
        self.jobs = None
        self.busy = False
        self._build()
        if self.phone_on.get():
            self._phone_toggle()
        self.after(100, self._poll)
        if not self.cfg.get("installed"):
            self.log("Первый запуск: нажми «Установить компоненты».\n"
                     "Скачается около 15 ГБ, это займёт 30–90 минут.\n")

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

        ttk.Label(top, text="Переводчик:").grid(row=2, column=0, sticky="w", **pad)
        trow = ttk.Frame(top)
        trow.grid(row=2, column=1, columnspan=2, sticky="ew")
        trow.columnconfigure(3, weight=1)
        names = dict((v, n) for n, v in TRANSLATORS)
        self.translator = tk.StringVar(value=names[self.cfg.get("translator", "local")])
        ttk.Combobox(trow, textvariable=self.translator, values=[n for n, _ in TRANSLATORS],
                     state="readonly", width=34).grid(row=0, column=0, padx=6)
        self.key_label = ttk.Label(trow, text="Ключ Claude:")
        self.key_label.grid(row=0, column=2, padx=(12, 4))
        self.key = tk.StringVar(value=self.cfg.get("api_key", ""))
        self.key_entry = ttk.Entry(trow, textvariable=self.key, show="•")
        self.key_entry.grid(row=0, column=3, sticky="ew", padx=6)
        self.translator.trace_add("write", lambda *_: self._toggle_key())
        self._toggle_key()

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
        self.b_copy = ttk.Button(btns, text="Скопировать журнал", command=self.copy_log)
        for b in (self.b_setup, self.b_tr, self.b_ed, self.b_dub, self.b_stop, self.b_open,
                  self.b_copy):
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

    def copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.logbox.get("1.0", "end-1c"))
        self.set_status("Журнал скопирован — вставь его в сообщение (Ctrl+V)")

    def set_status(self, text, pct=None):
        self.status.configure(text=text)
        if pct is not None:
            self.bar.configure(value=pct)

    def _set_busy(self, busy):
        self.busy = busy
        for b in (self.b_setup, self.b_tr, self.b_ed, self.b_dub):
            b.configure(state="disabled" if busy else "normal")
        self.b_stop.configure(state="normal" if busy else "disabled")

    def _translator(self):
        return dict(TRANSLATORS).get(self.translator.get(), "local")

    def _toggle_key(self):
        for w in (self.key_label, self.key_entry):
            if self._translator() == "claude":
                w.grid()
            else:
                w.grid_remove()

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
                        translator=self._translator(),
                        honorifics=self.honor.get(), voice_gain=round(self.vgain.get(), 2),
                        bg_gain=round(self.bgain.get(), 2), last_video=self.video.get(),
                        last_ctx=ctx)
        save_cfg(self.cfg)
        return ctx

    # --- доступ с телефона ---
    @staticmethod
    def _ffmpeg_dir():
        exe = shutil.which("ffmpeg", path=child_env()["PATH"])
        return str(Path(exe).parent) if exe else None

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
            if not self.jobs:
                self.jobs = Jobs(self._library, self._phone_cmd,
                                 lambda: child_env(self.cfg.get("api_key", "")),
                                 lambda: self.busy, UPLOADS, self._add_to_library,
                                 lambda text: self.q.put(("log", text + "\n")))
            try:
                self.phone = PhoneServer(key, self._library, thumbs=Thumbs(
                    APP_DIR / "thumbs", self._ffmpeg_dir), jobs=self.jobs)
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
        tail = collections.deque(maxlen=80)
        for line in self.proc.stdout:
            self.q.put(("line", line))
            if not line.startswith("Progress "):
                tail.append(line.rstrip())
        rc = self.proc.wait()
        self.proc = None
        if self.stopping:
            raise StepFailed("Остановлено")
        if rc != 0:
            errors = [l.strip() for l in tail if l.lstrip().startswith(("ERROR", "error:"))]
            msg = f"Этап «{title}» завершился с ошибкой (код {rc})."
            reason = explain_failure(tail)
            if reason:
                msg += "\n\n" + reason
            if errors:
                msg += "\n\nСообщение: " + errors[-1][:300]
            raise StepFailed(msg + "\n\nПолный текст — кнопка «Скопировать журнал».")

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
                elif kind == "step":
                    self.step, self.dl_name = data, ""
                    self._show_progress(0)
                elif kind == "progress":
                    self._show_progress(*data)
                elif kind == "done":
                    self.step = None
                    self._set_busy(False)
                    self.set_status("Готово", 100)
                    if data:
                        data()
                elif kind == "fail":
                    self.step = None
                    self._set_busy(False)
                    self.set_status("Ошибка" if data != "Остановлено" else "Остановлено")
                    self.log(f"\n✖ {data}\n")
                    if data != "Остановлено":
                        messagebox.showerror(APP_NAME, data)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _show_progress(self, frac, detail=""):
        """Прогресс шага установки -> общий процент установки."""
        if not self.step:
            return
        title, start, span = self.step
        total = start + span * min(max(frac, 0), 1)
        self.set_status(f"Установка {total:.0f}% — {title}" + (f": {detail}" if detail else ""),
                        total)

    def _handle_line(self, line):
        s = line.rstrip()
        m = PIP_PROGRESS_RE.match(s)
        if m:
            cur, size = int(m[1]), int(m[2])
            if size:
                self._show_progress(cur / size, f"{self.dl_name} — {cur * 100 // size}% "
                                                f"({mb(cur)} из {mb(size)})")
            return
        m = PIP_DOWNLOAD_RE.match(s)
        if m and self.step:
            self.dl_name = m[1].split("/")[-1].split("-")[0]
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
                APP_NAME, "Будет скачано около 15 ГБ (Python, PyTorch, нейросети, переводчик).\n"
                          "Это займёт 20–60 минут. Продолжить?"):
            return
        self.run_task(self._setup, lambda: messagebox.showinfo(
            APP_NAME, "Всё установлено! Можно выбирать серию."))

    def _download(self, urls, dest):
        """Скачивание с процентами; при ошибке пробует следующее зеркало."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        last = None
        for url in urls:
            self.q.put(("log", f"Скачивание {url}\n"))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
                with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                    size = int(r.headers.get("Content-Length") or 0)
                    done, shown = 0, 0.0
                    while True:
                        if self.stopping:
                            raise StepFailed("Остановлено")
                        buf = r.read(1 << 18)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        if time.monotonic() - shown > 0.3:
                            shown = time.monotonic()
                            detail = (f"{done * 100 // size}% ({mb(done)} из {mb(size)})" if size
                                      else mb(done))
                            self.q.put(("progress", (done / size if size else 0, detail)))
                if size and done < size:
                    raise OSError(f"файл скачан не полностью ({mb(done)} из {mb(size)})")
                part.replace(dest)
                return
            except (urllib.error.URLError, OSError) as e:
                last = e
                self.q.put(("log", f"Не получилось: {e}\n"))
        raise StepFailed(f"Не удалось скачать {dest.name}: {last}. Проверь интернет и попробуй снова.")

    def _install_python(self):
        exe = APP_DIR / "python-3.11.9-amd64.exe"
        self._download(PY_URLS, exe)
        self.q.put(("progress", (0.5, "установка без окон, около минуты")))
        self._stream("Установка Python 3.11",
                     [str(exe), "/quiet", "InstallAllUsers=0", f"TargetDir={PY_DIR}",
                      "Include_launcher=0", "Include_test=0", "Include_doc=0",
                      "PrependPath=0", "Shortcuts=0", "AssociateFiles=0"])
        exe.unlink(missing_ok=True)

    def _install_ffmpeg(self):
        z = APP_DIR / "ffmpeg.zip"
        self._download(FFMPEG_URLS, z)
        self.q.put(("progress", (1, "распаковка")))
        try:
            with zipfile.ZipFile(z) as arc:
                names = [n for n in arc.namelist()
                         if n.lower().endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe"))]
                if len(names) < 2:
                    raise StepFailed("В архиве ffmpeg нет ffmpeg.exe и ffprobe.exe.")
                (FFMPEG_DIR / "bin").mkdir(parents=True, exist_ok=True)
                for n in names:
                    with arc.open(n) as src, open(FFMPEG_DIR / "bin" / Path(n).name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except zipfile.BadZipFile:
            raise StepFailed("Архив ffmpeg повреждён. Нажми «Установить компоненты» ещё раз.")
        finally:
            z.unlink(missing_ok=True)
        self.q.put(("log", f"ffmpeg установлен в {FFMPEG_DIR / 'bin'}\n"))

    def _ensure_ollama(self):
        if ollama_up():
            return
        exe = ollama_exe()
        if not exe:
            raise StepFailed("Ollama не установилась. Нажми «Установить компоненты» ещё раз.")
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW, env=child_env())
        for _ in range(60):
            if self.stopping:
                raise StepFailed("Остановлено")
            time.sleep(1)
            if ollama_up():
                return
        raise StepFailed("Ollama установлена, но не запускается. Перезапусти компьютер и "
                         "снова нажми «Установить компоненты».")

    def _install_ollama(self):
        exe = APP_DIR / "OllamaSetup.exe"
        self._download(OLLAMA_URLS, exe)
        self.q.put(("progress", (0.9, "установка без окон, 1–2 минуты")))
        self._stream("Установка Ollama", [str(exe), "/VERYSILENT", "/NORESTART",
                                          "/SUPPRESSMSGBOXES"])
        exe.unlink(missing_ok=True)
        self._ensure_ollama()

    def _pull_model(self, model):
        self._ensure_ollama()
        if model in ollama_models():
            return
        self.q.put(("log", f"\n▶ Скачивание модели перевода {model}\n"))
        body = json.dumps({"model": model, "stream": True}).encode("utf-8")
        req = urllib.request.Request(OLLAMA_API + "/api/pull", data=body,
                                     headers={"Content-Type": "application/json"})
        shown = 0.0
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                for raw in r:
                    if self.stopping:
                        raise StepFailed("Остановлено")
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        continue
                    if msg.get("error"):
                        raise StepFailed(f"Не удалось скачать модель: {msg['error']}. Если "
                                         "серверы Ollama недоступны — включи VPN и попробуй снова.")
                    total, done = msg.get("total") or 0, msg.get("completed") or 0
                    if total > 50 * 2**20 and time.monotonic() - shown > 0.3:
                        shown = time.monotonic()
                        self.q.put(("progress", (done / total, f"{done * 100 // total}% "
                                                 f"({mb(done)} из {mb(total)})")))
                    elif msg.get("status") and not total:
                        self.q.put(("log", msg["status"] + "\n"))
        except (urllib.error.URLError, OSError) as e:
            raise StepFailed(f"Обрыв при скачивании модели ({e}). Нажми «Установить компоненты» "
                             "ещё раз — скачивание продолжится с того же места.")
        if model not in ollama_models():
            raise StepFailed("Модель перевода не скачалась. Нажми «Установить компоненты» ещё раз.")

    def _choose_model(self):
        r = subprocess.run([str(VPY), "-c", VRAM_CHECK], capture_output=True, text=True,
                           creationflags=NO_WINDOW, env=child_env())
        try:
            vram = float(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            vram = 0
        model, size = next((m, s) for limit, m, s in LOCAL_MODELS if vram < limit)
        self.cfg["local_model"] = model
        save_cfg(self.cfg)
        self.q.put(("log", f"Память видеокарты: {vram:.1f} ГБ → модель перевода {model} ({size})\n"))
        if 0 < vram < 6:
            self.q.put(("log", "Видеокарта с небольшой памятью: включён экономный режим. Часть "
                               "модели перевода будет работать на процессоре — перевод серии "
                               "займёт примерно 25–50 минут, озвучка при нехватке памяти "
                               "продолжится на процессоре.\n"))
        return model

    def _setup(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(APP_DIR).free
        if free < 21 * 2**30 and not self.cfg.get("installed"):
            raise StepFailed(f"На диске свободно {mb(free)}, а для установки нужно около 21 ГБ. "
                             "Освободи место и нажми «Установить компоненты» ещё раз.")
        if VPY.exists() and not python_ok([str(VPY)]):
            self.q.put(("log", "Окружение создано неподходящим Python — пересоздаю.\n"))
            shutil.rmtree(VENV, ignore_errors=True)
        py = find_python()
        has_ffmpeg = (shutil.which("ffmpeg", path=child_env()["PATH"])
                      and shutil.which("ffprobe", path=child_env()["PATH"]))
        net = {}  # заполняется шагом проверки серверов, читается следующими шагами

        def pip(*extra):
            cmd = [str(VPY), "-m", "pip", "install", "--disable-pip-version-check",
                   "--timeout", "60", "--retries", "10", "-i", net["pypi"]]
            # --progress-bar raw есть с pip 24.1, --resume-retries — с 25.1
            cmd += ["--progress-bar", "raw" if net["pip"] >= (24, 1) else "off"]
            if net["pip"] >= (25, 1):
                cmd += ["--resume-retries", "10"]
            return cmd + list(extra)

        def torch_args():
            kind, url = net["torch"]
            return ["--extra-index-url", url] if kind == "index" else ["-f", url]

        def check_servers():
            pypi, torch = pick_sources()
            if not pypi:
                raise StepFailed("Не открывается ни pypi.org, ни его зеркала — похоже, их блокирует "
                                 "провайдер или антивирус. Включи VPN (или выключи, если он уже "
                                 "включён) и нажми «Установить компоненты» ещё раз.")
            if not torch:
                raise StepFailed("Не открывается download.pytorch.org и его зеркала. Включи VPN "
                                 "(или выключи, если он уже включён) и попробуй ещё раз.")
            net.update(pypi=pypi, torch=torch, pip=(0, 0))
            self.q.put(("log", f"Пакеты: {pypi}\nPyTorch: {torch[1]}\n"))

        def upgrade_pip():
            try:
                self._stream("Обновление pip", pip("--upgrade", "pip"))
            except StepFailed as e:
                if self.stopping:
                    raise
                self.q.put(("log", f"pip обновить не удалось, продолжаю со старым ({e})\n"))
            r = subprocess.run([str(VPY), "-c", "import pip;print(pip.__version__)"],
                               capture_output=True, text=True, creationflags=NO_WINDOW)
            nums = re.findall(r"\d+", r.stdout)[:2]
            net["pip"] = tuple(int(n) for n in nums) if len(nums) == 2 else (0, 0)

        # (заголовок, вес в общем проценте, действие); вес ~ время шага
        steps = []
        if not py:
            steps.append(("Python 3.11 (~25 МБ)", 3, self._install_python))
        if not has_ffmpeg:
            steps.append(("ffmpeg (~100 МБ)", 5, self._install_ffmpeg))
        if not VPY.exists():
            steps.append(("Создание окружения", 2, lambda: self._stream(
                "Создание окружения", (find_python() or []) + ["-m", "venv", str(VENV)])))
        steps += [
            ("Проверка доступа к серверам", 1, check_servers),
            ("Обновление pip", 2, upgrade_pip),
            ("PyTorch с поддержкой видеокарты (~3 ГБ)", 45, lambda: self._stream(
                "PyTorch", pip(*TORCH_PINS, *torch_args()))),
            ("Нейросети и библиотеки", 40, lambda: self._stream(
                "Нейросети и библиотеки", pip(*REQS, *TORCH_PINS, *torch_args()))),
            ("Проверка видеокарты", 2, lambda: self._stream(
                "Проверка видеокарты", [str(VPY), "-c", GPU_CHECK])),
        ]
        if not ollama_exe():
            steps.append(("Локальный переводчик Ollama (~1 ГБ)", 8, self._install_ollama))
        steps.append(("Модель перевода (~5 ГБ)", 30, lambda: self._pull_model(self._choose_model())))
        total = sum(w for _, w, _ in steps)
        done = 0
        for n, (title, weight, action) in enumerate(steps, 1):
            label = f"шаг {n} из {len(steps)}, {title}"
            self.q.put(("step", (label, done * 100 / total, weight * 100 / total)))
            if not py and title.startswith("Создание") and not find_python():
                raise StepFailed("Python установлен, но не запускается. Перезапусти AnimeDub "
                                 "и снова нажми «Установить компоненты».")
            action()
            done += weight
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

    def _dub_cmd(self, video, lang, until=None):
        cfg = self.cfg
        local = cfg.get("translator", "local") == "local"
        ctx = work_dir(video) / "context.txt"
        cmd = [str(VPY), str(dub_script()), video, "--lang", lang, "--context", str(ctx),
               "--voice-gain", str(cfg.get("voice_gain", 1.0)),
               "--bg-gain", str(cfg.get("bg_gain", 1.0)),
               "--translator", "local" if local else "claude"]
        if cfg.get("honorifics"):
            cmd.append("--keep-honorifics")
        if local:
            cmd += ["--local-model", cfg.get("local_model", "qwen2.5:7b")]
        if until:
            cmd += ["--until", until]
        return cmd

    def _phone_cmd(self, video, lang):
        """Команда для озвучки, запущенной с телефона: целиком, без редактора перевода."""
        if not self.cfg.get("installed") or not VPY.exists():
            raise RuntimeError("На компьютере не установлены компоненты — нажми там "
                               "«Установить компоненты».")
        local = self.cfg.get("translator", "local") == "local"
        if local and not self.cfg.get("local_model"):
            raise RuntimeError("На компьютере не установлен локальный переводчик — нажми там "
                               "«Установить компоненты».")
        if not local and not self.cfg.get("api_key"):
            raise RuntimeError("На компьютере выбран Claude, но нет ключа. Выбери там "
                               "«Локально — бесплатно» или впиши ключ.")
        ctx = work_dir(video) / "context.txt"
        if not ctx.exists():
            ctx.parent.mkdir(exist_ok=True)
            ctx.write_text(self.cfg.get("last_ctx", ""), encoding="utf-8")
        return self._dub_cmd(video, lang)

    def start_dub(self, until):
        if not self._ready():
            return
        if self.jobs and self.jobs.busy():
            messagebox.showwarning(APP_NAME, "Сейчас идёт озвучка, запущенная с телефона. "
                                             "Дождись её окончания или отмени на телефоне.")
            return
        ctx = self._save_settings()
        local = self._translator() == "local"
        if self._needs_translation() and not local and not self.cfg.get("api_key"):
            messagebox.showwarning(APP_NAME, "Для перевода через Claude нужен ключ "
                                             "(console.anthropic.com). Или выбери переводчик "
                                             "«Локально — бесплатно».")
            return
        if self._needs_translation() and local and not self.cfg.get("local_model"):
            messagebox.showwarning(APP_NAME, "Локальный переводчик ещё не установлен. "
                                             "Нажми «Установить компоненты».")
            return
        video = self.video.get()
        wd = work_dir(video)
        wd.mkdir(exist_ok=True)
        self._add_to_library(Path(video).parent)
        self._ctx_file().write_text(ctx, encoding="utf-8")
        cmd = self._dub_cmd(video, dict(LANGS)[self.lang.get()], until)
        if self._needs_translation():
            self.log("Переводчик: " + (f"локально, {self.cfg['local_model']}" if local
                                       else "Claude") + "\n")
        if until:
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
