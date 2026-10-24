#!/usr/bin/env python3
"""
Конвейер дубляжа аниме голосами персонажей (для личного просмотра).
Запускается программой AnimeDub.exe, но работает и из консоли:
  python dub.py "серия01.mkv" --lang ru --until translate
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("COQUI_TOS_AGREED", "1")

SR = 44100
MODEL = "claude-sonnet-5-5"
STAGES = ["extract", "separate", "transcribe", "translate", "synth", "mix"]
CHARS_PER_SEC = 14

LANG_NAMES = {"ru": "русский", "en": "английский", "uk": "украинский", "es": "испанский",
              "de": "немецкий", "fr": "французский", "it": "итальянский", "pt": "португальский",
              "pl": "польский", "tr": "турецкий"}
XTTS_LANG = {"uk": "ru"}  # XTTS не знает украинский


def progress(stage, cur, total):
    print(f"@@PROGRESS {stage} {int(cur)} {int(total)}", flush=True)


def run(cmd):
    cmd = [str(c) for c in cmd]
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def save(p, data):
    Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def setup_cuda_dlls():
    """На Windows Whisper (ctranslate2) ищет cuDNN/cuBLAS — подсовываем те, что идут с PyTorch."""
    try:
        import torch
        lib = Path(torch.__file__).parent / "lib"
        if lib.exists():
            os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(lib))
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


# ---------- 1. извлечение звука ----------
def stage_extract(video, work):
    out = work / "audio.wav"
    if not out.exists():
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vn", "-ac", "2", "-ar", SR, out])


# ---------- 2. разделение голос / фон ----------
def sep_paths(work):
    d = work / "sep" / "htdemucs" / "audio"
    return d / "vocals.wav", d / "no_vocals.wav"


def stage_separate(work):
    vocals, _ = sep_paths(work)
    if not vocals.exists():
        run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
             "-o", work / "sep", work / "audio.wav"])


# ---------- 3. распознавание ----------
def transcribe_with(dev, vocals):
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device=dev,
                         compute_type="float16" if dev == "cuda" else "int8")
    segs, info = model.transcribe(str(vocals), language="ja", vad_filter=True, beam_size=5)
    lines = []
    for s in segs:
        progress("transcribe", s.end, info.duration)
        t = s.text.strip()
        if t:
            lines.append({"id": len(lines), "start": round(s.start, 2),
                          "end": round(s.end, 2), "ja": t, "tr": ""})
            print(f"[{s.start:7.1f}] {t}", flush=True)
    return lines


def stage_transcribe(work):
    out = work / "lines.json"
    if out.exists():
        return
    dev = setup_cuda_dlls()
    vocals, _ = sep_paths(work)
    try:
        lines = transcribe_with(dev, vocals)
    except (RuntimeError, OSError) as e:
        if dev != "cuda":
            raise
        print(f"Whisper на видеокарте не запустился ({e}). Переключаюсь на процессор.", flush=True)
        lines = transcribe_with("cpu", vocals)
    save(out, lines)


# ---------- 4. перевод ----------
SYSTEM = """Ты — локализатор аниме для дубляжа. Переводишь реплики с японского на {lang}.

Главное — звучать как живая речь, а не как перевод:
- Передавай смысл, эмоцию и подтекст, а не слова. Перестраивай фразы так, как сказал бы носитель.
- Сохраняй характер персонажа: грубый говорит грубо, застенчивый — сбивчиво, ребёнок — по-детски.
- Междометия и реакции адаптируй ("えっ?!" → "Чего?!", "うそ…" → "Да ладно…"), а не транслитерируй.
- Шутки, каламбуры и культурные отсылки заменяй на понятные зрителю аналоги.
- Японские обращения (-сан, -кун, -тян, -сэмпай): {honorifics}
- Не добавляй того, чего нет в оригинале, и не смягчай эмоции.
- ДЛИНА КРИТИЧНА: каждая реплика должна уложиться в max_chars символов, иначе её не успеют произнести. Если не влезает — сокращай, сохраняя суть.
- Текст получен распознаванием речи и может содержать ошибки — опирайся на контекст сцены.
{context}
Ответь ТОЛЬКО JSON-массивом: [{{"id": 0, "tr": "..."}}, ...] — без пояснений и markdown."""


def extract_json(text):
    a, b = text.find("["), text.rfind("]")
    return json.loads(text[a:b + 1])


def translated(l):
    """Реплика уже прошла перевод (пустой перевод тоже считается — например, шум)."""
    return bool(l["tr"]) or l.get("done", False)


def ask_chunk(client, system, msg, tries=3):
    for attempt in range(1, tries + 1):
        try:
            resp = client.messages.create(model=MODEL, max_tokens=8000, system=system,
                                          messages=[{"role": "user", "content": msg}])
            text = "".join(b.text for b in resp.content if b.type == "text")
            return {d["id"]: d["tr"] for d in extract_json(text)}
        except (ValueError, KeyError, TypeError) as e:
            if attempt == tries:
                raise
            print(f"Не удалось разобрать ответ ({e}). Повтор {attempt + 1} из {tries}...", flush=True)


def stage_translate(work, lang, ctx_file, keep_honorifics):
    path = work / "lines.json"
    lines = load(path)
    if all(translated(l) for l in lines):
        return
    import anthropic
    client = anthropic.Anthropic()
    context = ""
    if ctx_file and Path(ctx_file).exists():
        txt = Path(ctx_file).read_text(encoding="utf-8").strip()
        if txt:
            context = "\nСведения о тайтле и персонажах (учитывай манеру речи):\n" + txt
    system = SYSTEM.format(
        lang=LANG_NAMES.get(lang, lang), context=context,
        honorifics=("оставляй, если они важны для отношений персонажей" if keep_honorifics
                    else "заменяй естественными аналогами или убирай"))

    CHUNK = 40
    for i in range(0, len(lines), CHUNK):
        progress("translate", i, len(lines))
        chunk = [l for l in lines[i:i + CHUNK] if not translated(l)]
        if not chunk:
            continue
        prev = [{"ja": l["ja"], "tr": l["tr"]} for l in lines[max(0, i - 12):i] if l["tr"]]
        payload = [{"id": l["id"], "ja": l["ja"],
                    "max_chars": max(4, int((l["end"] - l["start"]) * CHARS_PER_SEC))}
                   for l in chunk]
        nxt = [l["ja"] for l in lines[i + CHUNK:i + CHUNK + 8]]
        msg = ("Предыдущие реплики (уже переведены, для связности):\n"
               + json.dumps(prev, ensure_ascii=False)
               + "\n\nПереведи эти реплики:\n" + json.dumps(payload, ensure_ascii=False)
               + "\n\nДальше в сцене (не переводить, только контекст):\n"
               + json.dumps(nxt, ensure_ascii=False))
        by_id = ask_chunk(client, system, msg)
        for l in chunk:
            l["tr"] = by_id.get(l["id"], "")
            l["done"] = l["id"] in by_id
            print(f"{l['ja']}\n  → {l['tr']}", flush=True)
        save(path, lines)
    progress("translate", len(lines), len(lines))


# ---------- 5. синтез ----------
def make_ref(vocals, sr, line, path):
    """Образец голоса = оригинальная реплика: несёт тембр и эмоцию именно этого момента."""
    import numpy as np
    import soundfile as sf
    a = vocals[int(line["start"] * sr):int(line["end"] * sr)]
    if len(a) < sr * 0.3:
        return False
    while len(a) < 3 * sr:
        a = np.concatenate([a, a])
    sf.write(path, a, sr)
    return True


def stage_synth(work, lang):
    import soundfile as sf
    dev = setup_cuda_dlls()
    from TTS.api import TTS
    lines = load(work / "lines.json")
    vocals_path, _ = sep_paths(work)
    vocals, vsr = sf.read(vocals_path, dtype="float32")
    if vocals.ndim > 1:
        vocals = vocals.mean(axis=1)

    out_dir, ref_dir = work / "tts", work / "refs"
    out_dir.mkdir(exist_ok=True)
    ref_dir.mkdir(exist_ok=True)

    todo = [l for l in lines if l["tr"].strip()]
    tts = None
    for n, l in enumerate(todo, 1):
        progress("synth", n, len(todo))
        wav = out_dir / f"{l['id']:04d}.wav"
        txt = out_dir / f"{l['id']:04d}.txt"
        if wav.exists() and txt.exists() and txt.read_text(encoding="utf-8") == l["tr"]:
            continue
        ref = ref_dir / f"{l['id']:04d}.wav"
        if not make_ref(vocals, vsr, l, ref):
            continue
        if tts is None:
            print("Загрузка XTTS-v2 (при первом запуске скачивается ~2 ГБ)...", flush=True)
            tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev)
        print(f"[{l['id']}] {l['tr']}", flush=True)
        tts.tts_to_file(text=l["tr"], speaker_wav=str(ref),
                        language=XTTS_LANG.get(lang, lang), file_path=str(wav))
        txt.write_text(l["tr"], encoding="utf-8")


# ---------- 6. сведение ----------
def sub_codec(video):
    """Субтитры mp4 (mov_text) нельзя скопировать в mkv — их перекодируем в srt."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s",
                        "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True)
    return "srt" if "mov_text" in r.stdout.split() else "copy"


def speed_up(src, rate, dst):
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-filter:a", f"atempo={rate:.3f}", "-ac", "1", "-ar", SR, dst])


def stage_mix(video, work, out_path, voice_gain, bg_gain):
    import librosa
    import numpy as np
    import soundfile as sf
    lines = load(work / "lines.json")
    _, nov = sep_paths(work)
    bg, _ = sf.read(nov, dtype="float32")
    if bg.ndim == 1:
        bg = np.stack([bg, bg], axis=1)
    dub = np.zeros(len(bg), dtype=np.float32)
    total = len(bg) / SR
    tmp, fast = work / "tmp_stretch.wav", work / "tmp_fast.wav"

    for k, l in enumerate(lines):
        progress("mix", k + 1, len(lines))
        f = work / "tts" / f"{l['id']:04d}.wav"
        if not f.exists() or not l["tr"].strip():
            continue
        y, _ = librosa.load(f, sr=SR, mono=True)
        y, _ = librosa.effects.trim(y, top_db=35)
        slot = l["end"] - l["start"]
        nxt = lines[k + 1]["start"] if k + 1 < len(lines) else total
        avail = max(slot, min(nxt - l["start"], slot + 0.8))
        dur = len(y) / SR
        if dur > avail:
            sf.write(tmp, y, SR)
            speed_up(tmp, min(dur / avail, 1.35), fast)
            y, _ = librosa.load(fast, sr=SR, mono=True)
        s = int(l["start"] * SR)
        e = min(s + len(y), len(dub))
        dub[s:e] += y[:e - s]

    mix = bg * bg_gain + dub[:, None] * voice_gain
    peak = np.abs(mix).max()
    if peak > 0.99:
        mix = mix / peak * 0.99
    dub_wav = work / "dub_audio.wav"
    sf.write(dub_wav, mix, SR)

    run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", dub_wav,
         "-map", "0:v:0", "-map", "1:a:0", "-map", "0:a:0?", "-map", "0:s?", "-map", "0:t?",
         "-c:v", "copy", "-c:s", sub_codec(video), "-c:t", "copy", "-c:a", "aac", "-b:a", "192k",
         "-metadata:s:a:0", "title=Дубляж", "-metadata:s:a:1", "title=Оригинал",
         "-disposition:a:0", "default", "-disposition:a:1", "0", out_path])
    print(f"\nГотово: {out_path}", flush=True)


def main():
    p = argparse.ArgumentParser(description="Дубляж аниме голосами персонажей")
    p.add_argument("video")
    p.add_argument("--lang", default="ru")
    p.add_argument("--context", help="txt-файл с описанием тайтла и персонажей")
    p.add_argument("--keep-honorifics", action="store_true")
    p.add_argument("--until", choices=STAGES)
    p.add_argument("--voice-gain", type=float, default=1.0)
    p.add_argument("--bg-gain", type=float, default=1.0)
    a = p.parse_args()

    video = Path(a.video)
    work = video.parent / f"work_{video.stem}"
    work.mkdir(exist_ok=True)
    out = video.parent / f"{video.stem}_dub_{a.lang}.mkv"

    steps = {
        "extract": lambda: stage_extract(video, work),
        "separate": lambda: stage_separate(work),
        "transcribe": lambda: stage_transcribe(work),
        "translate": lambda: stage_translate(work, a.lang, a.context, a.keep_honorifics),
        "synth": lambda: stage_synth(work, a.lang),
        "mix": lambda: stage_mix(video, work, out, a.voice_gain, a.bg_gain),
    }
    for name in STAGES:
        print(f"\n=== {name} ===", flush=True)
        steps[name]()
        if name == a.until:
            break


if __name__ == "__main__":
    main()
