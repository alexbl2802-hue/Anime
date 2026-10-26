#!/usr/bin/env python3
"""
AnimeDub без окна — для облачного сервера (Linux, можно без видеокарты).

Телефон подключается к серверу так же, как к ноутбуку: смотрит готовые серии,
загружает видео и запускает озвучку. Запуск:
  python headless.py              — работать (обычно как служба systemd)
  python headless.py --show       — показать адрес и ключ для приложения
  python headless.py --add-folder ПАПКА
  python headless.py --new-key    — сменить ключ (старый перестанет работать)
"""
import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from jobs import Jobs
from server import PORT, PhoneServer, Thumbs, addresses, new_key

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("ANIMEDUB_HOME", Path.home() / "animedub"))
CONFIG = HOME / "config.json"
DEFAULTS = {
    "port": PORT,
    "uploads": str(HOME / "videos"),  # сюда кладутся видео с телефона: videos/<аниме>/<серия>
    "library": [],                    # дополнительные папки с сериями
    "local_model": "qwen2.5:7b",
    "whisper": "large-v3-turbo",     # на процессоре в несколько раз быстрее large-v3
    "voice_gain": 1.0,
    "bg_gain": 1.0,
    "honorifics": False,
    "last_ctx": "",
}


def load_cfg():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    if not cfg.get("key"):
        cfg["key"] = new_key(16)  # сервер в интернете — ключ длиннее, чем у ноутбука
        save_cfg(cfg)
    return cfg


def save_cfg(cfg):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONFIG)
    os.chmod(CONFIG, 0o600)  # в файле ключ


def log(text):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), text, flush=True)


class Headless:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        Path(cfg["uploads"]).mkdir(parents=True, exist_ok=True)

    def library(self):
        """Каждая подпапка uploads — одно аниме; плюс папки, добавленные вручную."""
        root = Path(self.cfg["uploads"])
        found = [str(d) for d in sorted(root.iterdir()) if d.is_dir()] if root.is_dir() else []
        return found + [f for f in self.cfg["library"] if f not in found]

    def add_folder(self, folder):
        folder = str(Path(folder))
        if Path(folder).parent == Path(self.cfg["uploads"]):
            return  # и так видна
        with self.lock:
            if folder not in self.cfg["library"]:
                self.cfg["library"].append(folder)
                save_cfg(self.cfg)

    def make_cmd(self, video, lang):
        ctx = Path(video).parent / f"work_{Path(video).stem}" / "context.txt"
        if not ctx.exists():
            ctx.parent.mkdir(parents=True, exist_ok=True)
            ctx.write_text(self.cfg.get("last_ctx", ""), encoding="utf-8")
        cmd = [sys.executable, str(HERE / "dub.py"), video, "--lang", lang,
               "--context", str(ctx), "--translator", "local",
               "--local-model", self.cfg["local_model"], "--whisper", self.cfg["whisper"],
               "--voice-gain", str(self.cfg["voice_gain"]),
               "--bg-gain", str(self.cfg["bg_gain"])]
        if self.cfg.get("honorifics"):
            cmd.append("--keep-honorifics")
        return cmd

    @staticmethod
    def env():
        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
                   COQUI_TOS_AGREED="1")
        return env

    @staticmethod
    def ffmpeg_dir():
        exe = shutil.which("ffmpeg")
        return str(Path(exe).parent) if exe else None


def show(cfg):
    print("\nПодключение в приложении AnimeDub на телефоне:")
    addrs = addresses()
    tail = [a for a in addrs if a[1] == "Tailscale"]
    for ip, kind in tail or addrs:
        print(f"  Адрес: {ip}:{cfg['port']}   ({kind})")
    if not tail:
        print("  (Tailscale не найден — с телефона вне этой сети подключиться не выйдет,\n"
              "   см. README: «Сервер без ноутбука»)")
    print(f"  Ключ:  {cfg['key']}\n")


def main():
    p = argparse.ArgumentParser(description="AnimeDub-сервер без окна")
    p.add_argument("--show", action="store_true", help="показать адрес и ключ")
    p.add_argument("--add-folder", metavar="ПАПКА", help="добавить папку с сериями")
    p.add_argument("--new-key", action="store_true", help="выдать новый ключ")
    p.add_argument("--model", help="модель перевода Ollama, например qwen2.5:3b")
    a = p.parse_args()
    cfg = load_cfg()
    if a.new_key:
        cfg["key"] = new_key(16)
        save_cfg(cfg)
        print("Новый ключ выдан. Перезапусти службу: sudo systemctl restart animedub")
    if a.model:
        cfg["local_model"] = a.model
        save_cfg(cfg)
        print(f"Модель перевода: {a.model}")
    if a.add_folder:
        Headless(cfg).add_folder(a.add_folder)
        print(f"Папка добавлена: {a.add_folder}")
    if a.show or a.new_key:
        show(cfg)
    if a.show or a.new_key or a.add_folder or a.model:
        return

    h = Headless(cfg)
    jobs = Jobs(h.library, h.make_cmd, h.env, lambda: False, cfg["uploads"], h.add_folder, log)
    srv = PhoneServer(cfg["key"], h.library, port=cfg["port"],
                      thumbs=Thumbs(HOME / "thumbs", h.ffmpeg_dir), jobs=jobs)
    log(f"AnimeDub-сервер запущен на порту {cfg['port']}. Видео: {cfg['uploads']}")
    show(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    log("Остановка")
    srv.stop()


if __name__ == "__main__":
    main()
