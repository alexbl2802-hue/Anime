#!/usr/bin/env bash
# Установка AnimeDub-сервера на Ubuntu 22.04 / 24.04 (ARM или x86, видеокарта не нужна).
# Запуск на сервере одной командой:
#   curl -fsSL https://raw.githubusercontent.com/alexbl2802-hue/Anime/ccr-311f9029-7rdvqa/server/install.sh | bash
#
# Переменные (необязательно):
#   ANIMEDUB_MODEL=qwen2.5:3b   — модель перевода (по умолчанию qwen2.5:7b, лучше качество)
#   SKIP_TAILSCALE=1            — не ставить Tailscale
#   SKIP_MODEL=1 SKIP_SERVICE=1 — для проверки установки (CI)
set -euo pipefail

BRANCH="${ANIMEDUB_BRANCH:-ccr-311f9029-7rdvqa}"
REPO="${ANIMEDUB_REPO:-https://github.com/alexbl2802-hue/Anime.git}"
HOME_DIR="${ANIMEDUB_HOME:-$HOME/animedub}"
MODEL="${ANIMEDUB_MODEL:-qwen2.5:7b}"
APP="$HOME_DIR/app"
VENV="$HOME_DIR/venv"
PY="$VENV/bin/python"

say() { printf '\n\033[1;35m== %s ==\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "запусти от обычного пользователя (ubuntu), не от root — sudo скрипт вызовет сам"
command -v apt-get >/dev/null || die "нужна Ubuntu (или Debian)"

say "1/7 Системные пакеты"
sudo apt-get update -y -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q git ffmpeg python3 python3-venv \
    python3-dev build-essential curl libsndfile1 ca-certificates
PYV=$(python3 -c 'import sys; print(sys.version_info[1])')
case "$PYV" in 10|11|12) ;; *) die "нужен Python 3.10–3.12, а здесь 3.$PYV (возьми Ubuntu 22.04 или 24.04)";; esac

say "2/7 Файл подкачки (если памяти меньше 16 ГБ)"
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if [ "$MEM_GB" -lt 16 ] && ! swapon --show | grep -q .; then
    sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile \
        && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    echo "Добавлено 8 ГБ подкачки (оперативной памяти: ${MEM_GB} ГБ)"
else
    echo "Оперативной памяти: ${MEM_GB} ГБ — подкачка не нужна"
fi

say "3/7 Код AnimeDub (ветка $BRANCH)"
if [ -d "$APP/.git" ]; then
    git -C "$APP" fetch -q --depth 1 origin "$BRANCH" && git -C "$APP" checkout -q -f FETCH_HEAD
else
    git clone -q --depth 1 -b "$BRANCH" "$REPO" "$APP"
fi

say "4/7 Нейросети (PyTorch для процессора, распознавание, озвучка) — 10–20 минут"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$PY" -m pip install -q -U pip
PINS="torch==2.5.1 torchaudio==2.5.1"
if [ "$(uname -m)" = "x86_64" ]; then
    CPU_INDEX="--extra-index-url https://download.pytorch.org/whl/cpu"
    "$PY" -m pip install -q $PINS --index-url https://download.pytorch.org/whl/cpu
else
    CPU_INDEX=""  # для ARM колёса PyTorch на PyPI и так без CUDA
    "$PY" -m pip install -q $PINS
fi
"$PY" -m pip install -q coqui-tts faster-whisper demucs soundfile librosa $PINS $CPU_INDEX
"$PY" -c "import torch, faster_whisper, demucs, TTS; print('Нейросети на месте, PyTorch', torch.__version__)"

say "5/7 Переводчик Ollama и модель $MODEL"
if ! command -v ollama >/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
if ! curl -fs http://127.0.0.1:11434/api/version >/dev/null; then
    sudo systemctl enable --now ollama 2>/dev/null || (nohup ollama serve >/dev/null 2>&1 &)
    for _ in $(seq 30); do curl -fs http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
fi
if [ "${SKIP_MODEL:-0}" != 1 ]; then
    ollama pull "$MODEL"
fi
"$PY" "$APP/headless.py" --model "$MODEL" >/dev/null

say "6/7 Служба AnimeDub (запускается сама после перезагрузки)"
if [ "${SKIP_SERVICE:-0}" != 1 ]; then
    sudo tee /etc/systemd/system/animedub.service >/dev/null <<UNIT
[Unit]
Description=AnimeDub — озвучка аниме для приложения на телефоне
After=network-online.target ollama.service
Wants=network-online.target

[Service]
User=$USER
Environment=ANIMEDUB_HOME=$HOME_DIR
Environment=COQUI_TOS_AGREED=1
ExecStart=$PY $APP/headless.py
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable -q animedub
    sudo systemctl restart animedub
fi

say "7/7 Защищённая связь с телефоном (Tailscale)"
if [ "${SKIP_TAILSCALE:-0}" != 1 ]; then
    command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
    if ! tailscale ip -4 >/dev/null 2>&1; then
        echo "Сейчас появится ссылка — открой её на телефоне и войди тем же аккаунтом,"
        echo "что и в приложении Tailscale на телефоне."
        sudo tailscale up
    fi
    sudo systemctl restart animedub 2>/dev/null || true
fi

say "Готово"
"$PY" "$APP/headless.py" --show
echo "Журнал службы:      sudo journalctl -u animedub -f"
echo "Обновить AnimeDub:  запусти эту же команду установки ещё раз"
