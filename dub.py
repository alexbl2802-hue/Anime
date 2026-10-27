#!/usr/bin/env python3
"""
Конвейер дубляжа аниме голосами персонажей (для личного просмотра).
Запускается программой AnimeDub.exe, но работает и из консоли:
  python dub.py "серия01.mkv" --lang ru --until translate
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("COQUI_TOS_AGREED", "1")

SR = 44100
MODEL = "claude-sonnet-5-5"
LOCAL_MODEL = "qwen2.5:7b"
OLLAMA = "http://127.0.0.1:11434"
STAGES = ["extract", "separate", "transcribe", "translate", "synth", "mix"]
CHARS_PER_SEC = 14

LANG_NAMES = {"ru": "русский", "en": "английский", "uk": "украинский", "es": "испанский",
              "de": "немецкий", "fr": "французский", "it": "итальянский", "pt": "португальский",
              "pl": "польский", "tr": "турецкий"}
XTTS_LANG = {"uk": "ru"}  # XTTS не знает украинский


def progress(stage, cur, total):
    print(f"@@PROGRESS {stage} {int(cur)} {int(total)}", flush=True)


NAMES = {"ffmpeg": "ffmpeg (работа с видео)", "demucs": "отделение голоса от музыки"}


class StepError(RuntimeError):
    """Понятная причина сбоя для журнала и телефона (вместо длинной технической строки)."""


def run(cmd):
    cmd = [str(c) for c in cmd]
    print(">", " ".join(cmd), flush=True)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        prog = cmd[2] if cmd[1:2] == ["-m"] else Path(cmd[0]).stem
        raise StepError(f"Ошибка: {NAMES.get(prog, prog)} завершилось с кодом {e.returncode}. "
                        "Подробности — в журнале выше.") from None
    except FileNotFoundError:
        raise StepError(f"Ошибка: не найдена программа {cmd[0]}.") from None


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


LOW_VRAM_GB = 6  # меньше — экономный режим (например, ноутбучная RTX 3050 на 4 ГБ)


def vram_gb():
    """Память видеокарты в ГБ (0 — видеокарты нет)."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_properties(0).total_memory / 2**30
    except Exception:
        pass
    return 0.0


def low_vram():
    v = vram_gb()
    return 0 < v < LOW_VRAM_GB


def free_gpu():
    """Отдаём видеопамять следующему этапу."""
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def is_oom(e):
    text = str(e).lower()
    return "out of memory" in text or "cuda error" in text or "cublas" in text


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
    if vocals.exists():
        return
    cmd = [sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
           "-o", work / "sep", work / "audio.wav"]
    if low_vram():
        cmd[4:4] = ["--segment", "7"]  # куски поменьше — меньше видеопамяти
    try:
        run(cmd)
    except StepError:
        if setup_cuda_dlls() != "cuda":
            raise
        print("Не хватило видеопамяти для отделения голоса — продолжаю на процессоре "
              "(это медленнее, 5–15 минут).", flush=True)
        run(cmd[:4] + ["-d", "cpu"] + cmd[4:])


# ---------- 3. распознавание ----------
WHISPER = "large-v3"


def transcribe_with(dev, vocals, hotwords=None):
    from faster_whisper import WhisperModel
    if dev != "cuda":
        compute = "int8"
    elif low_vram():
        compute = "int8_float16"  # ~2 ГБ вместо ~4.5 ГБ, точность почти та же
    else:
        compute = "float16"
    try:
        model = WhisperModel(WHISPER, device=dev, compute_type=compute,
                             cpu_threads=os.cpu_count() or 4)
    except (ValueError, RuntimeError, OSError) as e:
        if WHISPER == "large-v3":
            raise
        print(f"Модель распознавания {WHISPER} недоступна ({e}), беру large-v3.", flush=True)
        model = WhisperModel("large-v3", device=dev, compute_type=compute,
                             cpu_threads=os.cpu_count() or 4)
    # condition_on_previous_text=False — Whisper не «зацикливается» на одной фразе
    # звук читаем сами: чтение файла внутри faster-whisper зависит от версии PyAV и ломается
    import librosa
    audio, _ = librosa.load(str(vocals), sr=16000, mono=True)
    # VAD мягче обычного: в аниме много криков и речи поверх музыки
    vad = {"threshold": 0.3, "min_silence_duration_ms": 500, "speech_pad_ms": 300}
    extra = {}
    if hotwords:  # имена из описания персонажей — Whisper будет писать их правильно
        extra["hotwords"] = " ".join(hotwords)
        extra["initial_prompt"] = "登場人物：" + "、".join(hotwords) + "。"
    segs, info = model.transcribe(audio, language="ja", vad_filter=True, vad_parameters=vad,
                                  beam_size=5, condition_on_previous_text=False, **extra)
    raw = []
    for s in segs:
        progress("transcribe", s.end, info.duration)
        t = s.text.strip()
        if t:
            raw.append({"start": round(s.start, 2), "end": round(s.end, 2), "ja": t,
                        "no_speech": s.no_speech_prob, "logprob": s.avg_logprob})
            print(f"[{s.start:7.1f}] {t}", flush=True)
    return clean_lines(raw)


# фразы, которые Whisper «слышит» в музыке и титрах, хотя их никто не говорит
PHANTOMS = ("ご視聴ありがとうございました", "ご視聴ありがとうございます", "チャンネル登録",
            "最後までご視聴", "字幕", "おやすみなさい。おやすみなさい")


def _norm(text):
    return re.sub(r"[\s、。!！?？…・「」『』~〜ー-]", "", text)


JA_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def clean_lines(raw):
    """Убираем фантомные фразы, зацикленные повторы, песни на английском и явный шум."""
    out, dropped, why = [], 0, []
    i = 0
    while i < len(raw):
        seg = raw[i]
        # серия одинаковых реплик подряд (3+) — повтор распознавания, оставляем первую
        j = i
        while j + 1 < len(raw) and _norm(raw[j + 1]["ja"]) == _norm(seg["ja"]):
            j += 1
        run = j - i + 1
        keep = [seg] if run >= 3 else raw[i:j + 1]
        if run - len(keep):
            dropped += run - len(keep)
            why.append(f"повтор ×{run}: {seg['ja'][:40]}")
        for k in keep:
            text = _norm(k["ja"])
            phantom = any(_norm(p) in text for p in PHANTOMS) and len(text) <= 20
            # только явный шум: Whisper почти уверен, что речи нет, и сам текст «мусорный»
            noise = k.get("no_speech", 0) > 0.85 and k.get("logprob", 0) < -1.3
            song = not JA_RE.search(k["ja"]) and len(re.findall(r"[A-Za-z]", k["ja"])) >= 3
            reason = ("фантом" if phantom else "шум" if noise else "песня" if song
                      else "пусто" if not text else None)
            if reason:
                dropped += 1
                why.append(f"{reason} [{k['start']:.0f} с]: {k['ja'][:40]}")
                continue
            out.append({"id": len(out), "start": k["start"], "end": k["end"],
                        "ja": k["ja"], "tr": ""})
        i = j + 1
    if dropped:
        print(f"Убрано лишних реплик: {dropped}", flush=True)
        for w in why[:40]:
            print("  — " + w, flush=True)
    return out


# ---------- кириллица -> катакана (для подсказки Whisper с именами) ----------
def to_katakana(word):
    """«Наруто» -> «ナルト». None, если слово не разбирается как японское имя."""
    rev = {}
    for kana, (_, cyr) in _KANA.items():
        rev.setdefault(cyr, kana)
        if "э" in cyr:
            rev.setdefault(cyr.replace("э", "е"), kana)  # «Хокаге» = «Хокагэ»
    for cons, kana in {"с": "ス", "к": "ク", "т": "ト", "ц": "ツ", "ф": "フ", "м": "ム",
                       "н": "ン", "й": "イ", "п": "プ", "б": "ブ", "г": "グ", "д": "ド",
                       "р": "ル", "х": "フ"}.items():
        rev.setdefault(cons, kana)
    w, out, i = word.lower().replace("ё", "ё"), [], 0
    while i < len(w):
        for size in (3, 2, 1):
            part = w[i:i + size]
            if part in rev:
                if size == 1 and part == "н" and i + 1 < len(w) and w[i + 1] in "аиуэоеяюё":
                    continue  # «на» и т.п. — не отдельная «ン»
                out.append(rev[part])
                i += size
                break
        else:
            return None
    return "".join(out) if len(out) >= 2 else None


def context_names(ctx_file):
    """Имена из описания персонажей: «Ирука — учитель…» -> [(«Ирука», «イルカ»)]."""
    if not ctx_file or not Path(ctx_file).exists():
        return []
    names = []
    for line in Path(ctx_file).read_text(encoding="utf-8").splitlines():
        head = re.split(r"\s[—–-]\s|:", line, maxsplit=1)[0] if re.search(r"\s[—–-]\s|:", line) else ""
        for word in re.findall(r"[А-ЯЁ][а-яё]+", head):
            kana = to_katakana(word)
            if kana and (word, kana) not in names:
                names.append((word, kana))
    return names[:20]


def stage_transcribe(work, ctx_file=None):
    out = work / "lines.json"
    if out.exists():
        print(f"Речь уже распознана раньше — беру готовое. Чтобы распознать и перевести заново, "
              f"удали папку {work}", flush=True)
        return
    dev = setup_cuda_dlls()
    if dev == "cuda":
        v = vram_gb()
        print(f"Видеопамять: {v:.1f} ГБ" + (" — экономный режим" if v < LOW_VRAM_GB else ""),
              flush=True)
    vocals, _ = sep_paths(work)
    hot = [kana for _, kana in context_names(ctx_file)]
    if hot:
        print("Подсказка для распознавания (имена из описания): " + "、".join(hot), flush=True)
    try:
        lines = transcribe_with(dev, vocals, hot)
    except (RuntimeError, OSError) as e:
        if dev != "cuda":
            raise
        print(f"Whisper на видеокарте не запустился ({e}). Переключаюсь на процессор.", flush=True)
        free_gpu()
        lines = transcribe_with("cpu", vocals, hot)
    free_gpu()
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


def max_chars(l):
    return max(4, int((l["end"] - l["start"]) * CHARS_PER_SEC))


def translate_system(lang, ctx_file, keep_honorifics, extra=""):
    context = ""
    if ctx_file and Path(ctx_file).exists():
        txt = Path(ctx_file).read_text(encoding="utf-8").strip()
        if txt:
            context = "\nСведения о тайтле и персонажах (учитывай манеру речи):\n" + txt
    return SYSTEM.format(
        lang=LANG_NAMES.get(lang, lang), context=context + extra,
        honorifics=("оставляй, если они важны для отношений персонажей" if keep_honorifics
                    else "заменяй естественными аналогами или убирай"))


def stage_translate(work, lang, ctx_file, keep_honorifics, translator="claude",
                    local_model=LOCAL_MODEL):
    path = work / "lines.json"
    lines = load(path)
    if all(translated(l) for l in lines):
        print("Перевод уже готов — беру его (поправить можно в «Редакторе перевода»).", flush=True)
        return
    if translator == "local":
        return translate_local(work, path, lines, lang, ctx_file, keep_honorifics, local_model)
    import anthropic
    client = anthropic.Anthropic()
    system = translate_system(lang, ctx_file, keep_honorifics)

    CHUNK = 40
    for i in range(0, len(lines), CHUNK):
        progress("translate", i, len(lines))
        chunk = [l for l in lines[i:i + CHUNK] if not translated(l)]
        if not chunk:
            continue
        prev = [{"ja": l["ja"], "tr": l["tr"]} for l in lines[max(0, i - 12):i] if l["tr"]]
        payload = [{"id": l["id"], "ja": l["ja"], "max_chars": max_chars(l)} for l in chunk]
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


# ---------- 4б. локальный перевод (Ollama, бесплатно) ----------
# Небольшая модель переводит хуже Claude, поэтому работа идёт в три шага:
# словарь имён -> перевод по сценам -> редакторский проход «как живая речь».
LINES_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "ja": {"type": "string"},
                       "tr": {"type": "string"}},
        "required": ["id", "ja", "tr"]}}},
    "required": ["lines"],
}
TERMS_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {
        "type": "object",
        "properties": {"ja": {"type": "string"}, "tr": {"type": "string"},
                       "note": {"type": "string"}},
        "required": ["ja", "tr"]}}},
    "required": ["terms"],
}

GLOSSARY_PROMPT = """Ниже — японские реплики серии аниме (распознаны автоматически, возможны ошибки).
Выпиши ТОЛЬКО имена персонажей, их прозвища и титулы, а также названия мест — то, что нужно
писать одинаково во всей серии. Приёмы, техники и обычные слова НЕ выписывай.
В поле ja — имя точно как в тексте, в tr — как его писать на языке «{lang}» (для русского —
кириллицей по системе Поливанова), в note — кто это и пол, если понятно из контекста.
Не выдумывай того, чего нет в тексте. Не больше 25 пунктов.{context}

Реплики:
{text}"""

EDIT_PROMPT = """Ты — редактор русского дубляжа аниме. Перед тобой черновой перевод сцены.
Сделай так, чтобы реплики звучали как живая разговорная речь на языке «{lang}», которую
актёр произнесёт естественно:
- убери канцелярит и дословность, перестрой фразы как сказал бы носитель;
- сохрани смысл, эмоцию и характер персонажа, не добавляй лишнего;
- имена и термины — строго по словарю;
- каждая реплика должна уложиться в max_chars символов, иначе сократи;
- если реплика уже звучит хорошо — оставь её как есть.
Верни все реплики с теми же id."""


# ---------- кана -> кириллица (Поливанов) / латиница (Хэпбёрн) ----------
_KANA = {}
for _row in """ア a а|イ i и|ウ u у|エ e э|オ o о|カ ka ка|キ ki ки|ク ku ку|ケ ke кэ|コ ko ко
|サ sa са|シ shi си|ス su су|セ se сэ|ソ so со|タ ta та|チ chi ти|ツ tsu цу|テ te тэ|ト to то
|ナ na на|ニ ni ни|ヌ nu ну|ネ ne нэ|ノ no но|ハ ha ха|ヒ hi хи|フ fu фу|ヘ he хэ|ホ ho хо
|マ ma ма|ミ mi ми|ム mu му|メ me мэ|モ mo мо|ヤ ya я|ユ yu ю|ヨ yo ё|ラ ra ра|リ ri ри
|ル ru ру|レ re рэ|ロ ro ро|ワ wa ва|ヲ o о|ン n н|ガ ga га|ギ gi ги|グ gu гу|ゲ ge гэ
|ゴ go го|ザ za дза|ジ ji дзи|ズ zu дзу|ゼ ze дзэ|ゾ zo дзо|ダ da да|ヂ ji дзи|ヅ zu дзу
|デ de дэ|ド do до|バ ba ба|ビ bi би|ブ bu бу|ベ be бэ|ボ bo бо|パ pa па|ピ pi пи|プ pu пу
|ペ pe пэ|ポ po по|ヴ vu ву|キャ kya кя|キュ kyu кю|キョ kyo кё|シャ sha ся|シュ shu сю
|ショ sho сё|チャ cha тя|チュ chu тю|チョ cho тё|ニャ nya ня|ニュ nyu ню|ニョ nyo нё
|ヒャ hya хя|ヒュ hyu хю|ヒョ hyo хё|ミャ mya мя|ミュ myu мю|ミョ myo мё|リャ rya ря
|リュ ryu рю|リョ ryo рё|ギャ gya гя|ギュ gyu гю|ギョ gyo гё|ジャ ja дзя|ジュ ju дзю
|ジョ jo дзё|ビャ bya бя|ビュ byu бю|ビョ byo бё|ピャ pya пя|ピュ pyu пю|ピョ pyo пё
|ファ fa фа|フィ fi фи|フェ fe фэ|フォ fo фо|ティ ti ти|ディ di ди|ウィ wi ви|ウェ we вэ
|シェ she сэ|ジェ je дзэ|チェ che тэ""".replace("\n", "").split("|"):
    _k, _lat, _cyr = _row.split()
    _KANA[_k] = (_lat, _cyr)
CYRILLIC = {"ru", "uk"}
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")
LATIN_RE = re.compile(r"[A-Za-z]")


def transliterate(text, lang):
    """Имя, записанное каной -> «Наруто» / «Naruto». None, если там не только кана."""
    kata = "".join(chr(ord(c) + 0x60) if "\u3041" <= c <= "\u3096" else c
                   for c in text.strip().replace("・", " ").replace("=", " "))
    cyr = lang in CYRILLIC
    out, i, double = [], 0, False
    while i < len(kata):
        c = kata[i]
        if c == " ":
            out.append(" ")
            i += 1
            continue
        if c == "ッ":
            double = True
            i += 1
            continue
        if c == "ー":  # долгота — опускаем
            i += 1
            continue
        pair = _KANA.get(kata[i:i + 2]) if len(kata[i:i + 2]) == 2 else None
        lat, ru = pair if pair else _KANA.get(c, (None, None))
        if lat is None:
            return None
        step = 2 if pair else 1
        prev = out[-1] if out else ""
        if c == "ウ" and not pair and prev and prev[-1] in "ouоуёю":
            i += step  # オウ/ユウ — долгая гласная
            continue
        if c == "イ" and not pair and cyr and prev and prev[-1] in "аоуэ":
            ru = "й"  # カイ -> кай
        syl = ru if cyr else lat
        if double:
            syl = ("т" if cyr and syl.startswith("ч") else syl[0]) + syl
            double = False
        out.append(syl)
        i += step
    word = "".join(out).strip()
    return " ".join(w[:1].upper() + w[1:] for w in word.split()) or None


def bad_translation(line, tr, echo, lang, terms=()):
    """Причина, по которой перевод реплики не годится, или None."""
    if not tr.strip():
        return "пусто"
    for t in terms:  # имя есть в оригинале — должно быть и в переводе (хотя бы основа слова)
        stem = t["tr"].split()[0][:max(3, len(t["tr"].split()[0]) - 2)].lower()
        if t["ja"] in line["ja"] and stem and stem not in tr.lower():
            return f"имя «{t['tr']}» переведено неправильно или потеряно"
    if lang not in ("ja", "zh") and CJK_RE.search(tr):
        return "иероглифы в переводе"
    if lang in CYRILLIC and LATIN_RE.search(tr) and not LATIN_RE.search(line["ja"]):
        return "латиница в русском тексте"
    if echo is not None:
        import difflib
        a, b = _norm(echo), _norm(line["ja"])
        if a and b and difflib.SequenceMatcher(None, a, b).ratio() < 0.5:
            return "перевод съехал на другую реплику"
    return None


SHORTEN_PROMPT = """Ты — редактор дубляжа. Эти реплики слишком длинные: актёр не успеет их
произнести. Сократи каждую до max_chars символов на языке «{lang}», сохранив смысл, эмоцию
и имена. Верни для каждой её id, японский оригинал (ja) без изменений и сокращённый перевод (tr)."""


def ollama_exe():
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    for c in [local / "Programs" / "Ollama" / "ollama.exe", Path("ollama.exe"), Path("ollama")]:
        if c.is_absolute() and c.exists():
            return str(c)
    from shutil import which
    return which("ollama")


def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def ensure_ollama():
    if ollama_up():
        return
    exe = ollama_exe()
    if not exe:
        raise SystemExit("Локальный переводчик не установлен. Нажми «Установить компоненты».")
    print("Запуск локального переводчика...", flush=True)
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x08000000 if os.name == "nt" else 0)
    for _ in range(60):
        time.sleep(1)
        if ollama_up():
            return
    raise SystemExit("Локальный переводчик не запустился.")


def ollama_chat(model, system, user, schema, keep_alive="10m", tries=3):
    body = json.dumps({
        "model": model, "stream": False, "format": schema, "keep_alive": keep_alive,
        "options": {"temperature": 0.3, "num_ctx": 8192},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode("utf-8")
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=900) as r:
                data = json.loads(r.read().decode("utf-8"))
            return json.loads(data["message"]["content"])
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            if attempt == tries:
                raise
            print(f"Переводчик ответил с ошибкой ({e}). Повтор {attempt + 1} из {tries}...", flush=True)


def ollama_unload(model):
    """Освобождаем видеопамять для озвучки."""
    try:
        body = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
    except (urllib.error.URLError, OSError):
        pass


def scenes(lines, gap=4.0, limit=25):
    """Делим серию на сцены по паузам; длинные сцены — на куски до limit реплик."""
    out, cur = [], []
    for l in lines:
        if cur and (l["start"] - cur[-1]["end"] > gap or len(cur) >= limit):
            out.append(cur)
            cur = []
        cur.append(l)
    if cur:
        out.append(cur)
    return out


def build_glossary(work, lines, model, lang, ctx_file):
    path = work / "glossary.json"
    if path.exists():
        return load(path)
    text = "\n".join(l["ja"] for l in lines)[:6000]
    context = ""
    if ctx_file and Path(ctx_file).exists():
        txt = Path(ctx_file).read_text(encoding="utf-8").strip()
        if txt:
            context = "\n\nОписание от зрителя:\n" + txt
    print("Составляю словарь имён и терминов...", flush=True)
    try:
        terms = ollama_chat(model, "Ты внимательный переводчик аниме.", GLOSSARY_PROMPT.format(
            lang=LANG_NAMES.get(lang, lang), context=context, text=text), TERMS_SCHEMA)["terms"]
    except Exception as e:  # словарь полезен, но не обязателен
        print(f"Словарь составить не удалось ({e}), перевожу без него.", flush=True)
        terms = []
    clean = []
    for t in terms:
        ja, tr = str(t.get("ja", "")).strip(), str(t.get("tr", "")).strip()
        if not ja or not tr or ja not in text:
            continue  # пусто или выдумано
        exact = transliterate(ja, lang)
        if exact:
            tr = exact  # имя каной — точно по таблице, а не как придумала модель
        elif CJK_RE.search(tr) or (lang in CYRILLIC and LATIN_RE.search(tr)) or tr == ja:
            continue
        clean.append({"ja": ja, "tr": tr, "note": str(t.get("note", "")).strip()[:60]})
    # имена из описания зрителя — главнее всего, что придумала модель
    fixed = {}
    for ru, kana in context_names(ctx_file):
        fixed[kana] = {"ja": kana, "tr": ru, "note": "из описания персонажей"}
    # катакана перед обращением (イルカ先生, ナルト君) — это имя, даже если это ещё и слово
    for m in re.finditer(r"([\u30a1-\u30fa\u30fc]{2,})(?=先生|せんせい|さん|君|くん|ちゃん|様|さま|殿)", text):
        kana = m.group(1)
        exact = transliterate(kana, lang)
        if exact and kana not in fixed:
            fixed[kana] = {"ja": kana, "tr": exact, "note": "имя (стоит перед обращением)"}
    clean = list(fixed.values()) + [t for t in clean if t["ja"] not in fixed]
    terms = clean[:25]
    for t in terms:
        print(f"  {t['ja']} → {t['tr']}" + (f" ({t['note']})" if t.get("note") else ""), flush=True)
    save(path, terms)
    return terms


def _ask_lines(model, system, msg):
    """{id: (ja-повтор, перевод)} из ответа модели."""
    out = {}
    for d in ollama_chat(model, system, msg, LINES_SCHEMA).get("lines", []):
        if isinstance(d, dict) and isinstance(d.get("id"), int):
            out[d["id"]] = (str(d.get("ja", "")), str(d.get("tr", "")).strip())
    return out


def _context_msg(lines, pos, count, payload, title):
    prev = [{"ja": l["ja"], "tr": l["tr"]} for l in lines[max(0, pos - 8):pos] if l["tr"]]
    nxt = [l["ja"] for l in lines[pos + count:pos + count + 5]]
    return ("Предыдущие реплики (уже переведены, для связности):\n"
            + json.dumps(prev, ensure_ascii=False)
            + f"\n\n{title}\n" + json.dumps(payload, ensure_ascii=False)
            + "\n\nДальше в сцене (не переводить, только контекст):\n"
            + json.dumps(nxt, ensure_ascii=False))


def translate_local(work, path, lines, lang, ctx_file, keep_honorifics, model):
    ensure_ollama()
    terms = build_glossary(work, lines, model, lang, ctx_file)
    glossary = ""
    if terms:
        glossary = "\nСловарь (переводи строго так):\n" + "\n".join(
            f"- {t['ja']} → {t['tr']}" + (f" ({t['note']})" if t.get("note") else "") for t in terms)
    purity = (f"\n- Пиши только на языке «{LANG_NAMES.get(lang, lang)}»: никаких китайских или японских"
              " иероглифов и никакой латиницы в именах.")
    system = translate_system(lang, ctx_file, keep_honorifics, purity + glossary)
    system = system.replace("Ответь ТОЛЬКО JSON-массивом: [{\"id\": 0, \"tr\": \"...\"}, ...] — без пояснений и markdown.",
                            "Ответь JSON-объектом {\"lines\": [{\"id\": 0, \"ja\": \"оригинал\", "
                            "\"tr\": \"перевод\"}]}: для КАЖДОЙ реплики её id, её японский оригинал "
                            "без изменений и перевод именно этой реплики.")
    editor = (EDIT_PROMPT.format(lang=LANG_NAMES.get(lang, lang)) + purity + glossary
              + "\nДля каждой реплики верни её id, её японский оригинал (ja) без изменений и "
                "исправленный перевод (tr) именно этой реплики.")

    fixed = shortened = 0
    done_before = 0
    for scene in scenes(lines):
        progress("translate", done_before, len(lines))
        done_before += len(scene)
        todo = [l for l in scene if not translated(l)]
        if not todo:
            continue
        pos = lines.index(scene[0])
        payload = [{"id": l["id"], "ja": l["ja"], "max_chars": max_chars(l)} for l in todo]
        draft = _ask_lines(model, system, _context_msg(lines, pos, len(scene), payload,
                                                       "Переведи эти реплики:"))
        # редакторский проход по всей сцене
        review = [{"id": l["id"], "ja": l["ja"], "draft": draft.get(l["id"], ("", ""))[1],
                   "max_chars": max_chars(l)} for l in todo]
        try:
            edited = _ask_lines(model, editor, json.dumps(review, ensure_ascii=False))
        except Exception as e:  # черновик лучше, чем ничего
            print(f"Редактура сцены не удалась ({e}), оставляю черновик.", flush=True)
            edited = {}
        seen = {}  # перевод -> японский оригинал: одинаковый перевод разных реплик = сдвиг
        for l in todo:
            best, reason = "", "нет ответа"
            for got in (edited.get(l["id"]), draft.get(l["id"])):  # сначала редактура
                if got:
                    reason = bad_translation(l, got[1], got[0], lang, terms)
                    key = _norm(got[1]).lower()
                    if not reason and key in seen and _norm(seen[key]) != _norm(l["ja"]):
                        reason = "перевод совпал с соседней репликой — похоже, съехал"
                    if not reason:
                        best = got[1]
                        break
            # плохая реплика — переводим её отдельно (до двух раз)
            attempt = 0
            while reason and attempt < 2:
                attempt += 1
                one = [{"id": l["id"], "ja": l["ja"], "max_chars": max_chars(l)}]
                hint = f"Переведи ОДНУ эту реплику (прошлый перевод не подошёл: {reason}):"
                try:
                    got = _ask_lines(model, system, _context_msg(
                        lines, lines.index(l), 1, one, hint)).get(l["id"])
                except Exception:
                    got = None
                if got:
                    reason = bad_translation(l, got[1], None, lang, terms)
                    best = got[1] if not reason or not best else best
            if reason and best:  # последний шанс: вычистить иероглифы и латиницу
                best = CJK_RE.sub("", best)
                if lang in CYRILLIC and not LATIN_RE.search(l["ja"]):
                    best = " ".join(w for w in best.split() if not LATIN_RE.search(w))
                best = re.sub(r"\s+([,.!?…])", r"\1", best).strip()
            if attempt:
                fixed += 1
            l["tr"] = best
            l["done"] = True
            if best:
                seen[_norm(best).lower()] = l["ja"]
        # реплики, которые не влезут даже с ускорением, просим сократить
        long = [l for l in todo if len(l["tr"]) > max_chars(l) * 1.3]
        if long:
            ask = [{"id": l["id"], "ja": l["ja"], "tr": l["tr"], "max_chars": max_chars(l)}
                   for l in long]
            try:
                short = _ask_lines(model, SHORTEN_PROMPT.format(lang=LANG_NAMES.get(lang, lang))
                                   + glossary, json.dumps(ask, ensure_ascii=False))
            except Exception:
                short = {}
            for l in long:
                got = short.get(l["id"])
                if got and len(got[1]) < len(l["tr"]) and not bad_translation(l, got[1], None, lang, terms):
                    l["tr"] = got[1]
                    shortened += 1
        for l in todo:
            print(f"{l['ja']}\n  → {l['tr']}", flush=True)
        save(path, lines)
    if fixed:
        print(f"Переведено заново по одной: {fixed} реплик(и)", flush=True)
    if shortened:
        print(f"Сокращено длинных реплик, чтобы влезли по времени: {shortened}", flush=True)
    progress("translate", len(lines), len(lines))
    ollama_unload(model)


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


def cluster_speakers(embs, threshold=0.25):
    """Группы похожих голосов по «отпечаткам» реплик: {номер реплики в списке: группа}."""
    import numpy as np
    if len(embs) < 2:
        return {i: i for i in range(len(embs))}
    from sklearn.cluster import AgglomerativeClustering
    x = np.stack([e / (np.linalg.norm(e) + 1e-9) for e in embs])
    labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                     distance_threshold=threshold).fit_predict(x)
    return {i: int(g) for i, g in enumerate(labels)}


def speaker_refs(work, tts, lines, vocals, vsr):
    """Для каждой реплики — общий образец голоса её персонажа (до ~15 с его лучших реплик).
    Возвращает {id реплики: [пути к образцам]} или {} если не получилось."""
    import numpy as np
    import soundfile as sf
    raw_dir = work / "refs_raw"
    raw_dir.mkdir(exist_ok=True)
    cache = work / "speakers.json"
    good = [l for l in lines if l["end"] - l["start"] >= 1.0]
    if len(good) < 2:
        return {}
    try:
        groups = load(cache) if cache.exists() else None
        if groups is None or set(map(int, groups)) != {l["id"] for l in good}:
            model = tts.synthesizer.tts_model
            embs = []
            print("Определяю, кто говорит (группирую реплики по голосам)...", flush=True)
            for n, l in enumerate(good, 1):
                progress("synth", 0, 1)
                clip = vocals[int(l["start"] * vsr):int(l["end"] * vsr)]
                path = raw_dir / f"{l['id']:04d}.wav"
                sf.write(path, clip, vsr)
                _, emb = model.get_conditioning_latents(audio_path=[str(path)])
                embs.append(emb.detach().float().cpu().numpy().reshape(-1))
            labels = cluster_speakers(embs)
            groups = {str(l["id"]): labels[i] for i, l in enumerate(good)}
            save(cache, groups)
        by_group = {}
        for l in good:
            by_group.setdefault(groups[str(l["id"])], []).append(l)
        real = [g for g in by_group.values() if len(g) >= 2]
        print(f"Найдено голосов: {len(real)} (реплик с общим образцом: "
              f"{sum(len(g) for g in real)})", flush=True)
        out = {}
        for g in real:
            best, total = [], 0.0
            for l in sorted(g, key=lambda l: l["end"] - l["start"], reverse=True):
                path = raw_dir / f"{l['id']:04d}.wav"
                if not path.exists():
                    sf.write(path, vocals[int(l["start"] * vsr):int(l["end"] * vsr)], vsr)
                best.append((l["id"], str(path)))
                total += l["end"] - l["start"]
                if total >= 15 or len(best) >= 5:
                    break
            for l in g:
                out[l["id"]] = [p for i, p in best if i != l["id"]][:4]
        return out
    except Exception as e:  # noqa: BLE001 — без группировки озвучка всё равно работает
        print(f"Не удалось определить голоса ({e}) — образцом будет сама реплика.", flush=True)
        return {}


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
    pending = [l for l in todo if not ((out_dir / f"{l['id']:04d}.wav").exists()
               and (out_dir / f"{l['id']:04d}.txt").exists()
               and (out_dir / f"{l['id']:04d}.txt").read_text(encoding="utf-8") == l["tr"])]
    tts = None
    refs = {}

    def load_tts():
        nonlocal tts
        print("Загрузка XTTS-v2 (при первом запуске скачивается ~2 ГБ)...", flush=True)
        free_gpu()
        tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev)

    for n, l in enumerate(todo, 1):
        progress("synth", n, len(todo))
        wav = out_dir / f"{l['id']:04d}.wav"
        txt = out_dir / f"{l['id']:04d}.txt"
        if l not in pending:
            continue
        ref = ref_dir / f"{l['id']:04d}.wav"
        if not make_ref(vocals, vsr, l, ref):
            continue
        print(f"[{l['id']}] {l['tr']}", flush=True)
        for attempt in (1, 2):
            try:
                if tts is None:
                    load_tts()
                    if not refs:
                        refs = speaker_refs(work, tts, todo, vocals, vsr)
                # своя реплика (эмоция момента) + общий образец персонажа (тембр)
                speaker = [str(ref)] + refs.get(l["id"], [])
                tts.tts_to_file(text=l["tr"], speaker_wav=speaker if len(speaker) > 1 else str(ref),
                                language=XTTS_LANG.get(lang, lang), file_path=str(wav))
                break
            except RuntimeError as e:
                if dev != "cuda" or attempt == 2 or not is_oom(e):
                    raise
                print("Не хватило видеопамяти для озвучки — продолжаю на процессоре "
                      "(медленнее, но уже озвученное сохранено).", flush=True)
                tts, dev = None, "cpu"
                free_gpu()
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


def gap_mask(n, spans, sr, pad=0.15, fade=0.08):
    """1 там, где русской реплики нет (там оставим оригинальный голос), 0 — где она есть.
    Края плавные, чтобы переходы не щёлкали."""
    import numpy as np
    covered = np.zeros(n, dtype=np.float32)
    for a, b in spans:
        covered[max(0, int((a - pad) * sr)):min(n, int((b + pad) * sr))] = 1.0
    k = max(1, int(fade * sr))
    if k > 1:
        covered = np.convolve(covered, np.ones(k, dtype=np.float32) / k, mode="same")
    return 1.0 - np.clip(covered, 0.0, 1.0)


def stage_mix(video, work, out_path, voice_gain, bg_gain, keep_original=True):
    import librosa
    import numpy as np
    import soundfile as sf
    lines = load(work / "lines.json")
    voc_path, nov = sep_paths(work)
    bg, _ = sf.read(nov, dtype="float32")
    if bg.ndim == 1:
        bg = np.stack([bg, bg], axis=1)
    dub = np.zeros(len(bg), dtype=np.float32)
    spans = []
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
        spans.append((l["start"], max(l["end"], e / SR)))

    mix = bg * bg_gain + dub[:, None] * voice_gain
    if keep_original:
        # речь, которую не распознали или не озвучили, звучит оригиналом — без «немых» мест
        orig, _ = sf.read(voc_path, dtype="float32")
        if orig.ndim == 1:
            orig = np.stack([orig, orig], axis=1)
        n = min(len(orig), len(mix))
        mask = gap_mask(n, spans, SR)
        mix[:n] += orig[:n] * mask[:, None] * voice_gain
        kept = float((mask > 0.5).mean()) * 100
        print(f"Оригинальный голос оставлен там, где нет русской реплики (~{kept:.0f}% времени).",
              flush=True)
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
    global WHISPER
    p = argparse.ArgumentParser(description="Дубляж аниме голосами персонажей")
    p.add_argument("video")
    p.add_argument("--lang", default="ru")
    p.add_argument("--context", help="txt-файл с описанием тайтла и персонажей")
    p.add_argument("--keep-honorifics", action="store_true")
    p.add_argument("--translator", choices=["claude", "local"], default="claude")
    p.add_argument("--local-model", default=LOCAL_MODEL)
    p.add_argument("--whisper", default=WHISPER,
                   help="модель распознавания; на сервере без видеокарты — large-v3-turbo")
    p.add_argument("--until", choices=STAGES)
    p.add_argument("--voice-gain", type=float, default=1.0)
    p.add_argument("--bg-gain", type=float, default=1.0)
    p.add_argument("--no-original-gaps", action="store_true",
                   help="не оставлять оригинальный голос там, где нет русской реплики")
    a = p.parse_args()
    WHISPER = a.whisper
    try:  # на процессоре — все ядра
        import torch
        torch.set_num_threads(os.cpu_count() or 4)
    except Exception:
        pass

    video = Path(a.video)
    work = video.parent / f"work_{video.stem}"
    work.mkdir(exist_ok=True)
    out = video.parent / f"{video.stem}_dub_{a.lang}.mkv"

    steps = {
        "extract": lambda: stage_extract(video, work),
        "separate": lambda: stage_separate(work),
        "transcribe": lambda: stage_transcribe(work, a.context),
        "translate": lambda: stage_translate(work, a.lang, a.context, a.keep_honorifics,
                                             a.translator, a.local_model),
        "synth": lambda: stage_synth(work, a.lang),
        "mix": lambda: stage_mix(video, work, out, a.voice_gain, a.bg_gain,
                                 not a.no_original_gaps),
    }
    for name in STAGES:
        print(f"\n=== {name} ===", flush=True)
        steps[name]()
        if name == a.until:
            break


if __name__ == "__main__":
    try:
        main()
    except StepError as e:
        print(e, flush=True)
        sys.exit(1)
