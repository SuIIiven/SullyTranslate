#!/usr/bin/env python3
"""SullyTranslate: live subtitles + translation for your mic or anything playing on your speakers.

Pipeline: audio input (PipeWire/Pulse via parec on Linux, WASAPI via sounddevice on Windows)
-> Silero VAD -> faster-whisper (language ID + transcription) -> NLLB-200 translation (CTranslate2)
-> OBS text sources (--serve, driven by SullyTranslate.lua), always-on-top overlay, or terminal (--cli).
Everything runs on your machine; NVIDIA GPU if present, CPU otherwise.
"""

import argparse
import collections
import json
import locale
import os
import queue
import re
import subprocess
import sys
import threading
import time

APP = "SullyTranslate"
WINDOWS = os.name == "nt"
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_HOME", os.path.join(HERE, "models"))   # everything lives in the install folder
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")       # Windows without developer mode copies instead
SR = 16000
FRAME = 512                 # Silero VAD frame (32 ms @ 16 kHz)
CHUNK = FRAME * 8           # 256 ms read granularity
VAD_CONTEXT = FRAME * 32    # ~1 s of history fed to VAD (its RNN state resets per call)
PRE_ROLL = int(0.4 * SR)    # audio kept before speech onset so first syllable isn't cut
# Whole-utterance outputs Whisper invents on music/noise (normalized: lowercase, no punctuation).
HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching", "thank you so much for watching",
    "please subscribe", "subtitles by the amara org community", "bye",
}


# Translation models: key -> (Hugging Face repo, pinned revision). M2M-100 is MIT-licensed (commercial use
# OK); NLLB-200 translates better but is CC-BY-NC 4.0 (non-commercial use only).
MT_MODELS = {
    "m2m": ("jncraton/m2m100_1.2B-ct2-int8", "e50078df6be13a88592b70ea42f4d74c2082e448"),
    "m2m-small": ("jncraton/m2m100_418M-ct2-int8", "7c1b2620a4e58dacecbd8bf89cfd6da7eb9eb7b0"),
    "nllb": ("OpenNMT/nllb-200-3.3B-ct2-int8", None),
    "nllb-fast": ("OpenNMT/nllb-200-distilled-1.3B-ct2-int8", None),
}


def setup_gpu_libs():
    """Make the pip-installed cuBLAS/cuDNN findable by CTranslate2 (no-op for CPU-only installs)."""
    import site
    dirs = [os.path.join(sp, "nvidia", lib, "bin" if WINDOWS else "lib")
            for sp in site.getsitepackages() for lib in ("cublas", "cudnn")]
    dirs = [d for d in dirs if os.path.isdir(d)]
    if not dirs:
        return
    if WINDOWS:
        for d in dirs:
            os.add_dll_directory(d)
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        return
    if os.environ.get("_SULLYTRANSLATE_LIBS"):      # Linux: the loader only reads LD_LIBRARY_PATH at startup
        return
    env = dict(os.environ, _SULLYTRANSLATE_LIBS="1")
    env["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def resolve_hardware(args):
    """Fill in every 'auto' choice: GPU gets the most accurate models, CPU the fastest usable ones."""
    if args.device == "auto":
        import ctranslate2
        args.device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    gpu = args.device == "cuda"
    if args.compute_type == "auto":
        args.compute_type = "float16" if gpu else "int8"
    if args.model == "auto":
        args.model = "large-v3" if gpu else "large-v3-turbo"
    if args.mt_model == "auto":
        args.mt_model = "m2m" if gpu else "m2m-small"


# ---------------------------------------------------------------- audio capture

def _wasapi_devices():
    import sounddevice as sd
    apis = sd.query_hostapis()
    api = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), sd.default.hostapi)
    devs = [(i, d) for i, d in enumerate(sd.query_devices()) if d["hostapi"] == api and d["max_input_channels"] > 0]
    return devs, apis[api]["default_input_device"]


def list_inputs():
    """Capture devices as [{'id', 'name'}]; `id` is what capture() takes."""
    if WINDOWS:
        devs, _ = _wasapi_devices()
        return [{"id": d["name"], "name": d["name"]} for _, d in devs]
    out = subprocess.run(["pactl", "--format=json", "list", "sources"], capture_output=True, text=True)
    try:
        return [{"id": s["name"], "name": s.get("description") or s["name"]} for s in json.loads(out.stdout)]
    except ValueError:                                # old pactl without JSON output
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
        return [{"id": n, "name": n} for n in (ln.split("\t")[1] for ln in out.splitlines() if "\t" in ln)]


def default_input():
    """The system's default microphone."""
    if WINDOWS:
        devs, default = _wasapi_devices()
        return next((d["name"] for i, d in devs if i == default), devs[0][1]["name"] if devs else "")
    return subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True).stdout.strip()


def default_monitor():
    """What's playing on the speakers (Linux only; Windows has no monitor source)."""
    sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True).stdout.strip()
    return sink + ".monitor"


def capture(source, out_q, stop):
    """Push float32 mono 16 kHz chunks of exactly CHUNK samples into out_q; None when the input ends."""
    try:
        (_capture_sounddevice if WINDOWS else _capture_parec)(source, out_q, stop)
    except Exception as e:
        print(f"{APP}: audio input failed: {e}", file=sys.stderr, flush=True)
    finally:
        out_q.put(None)


def _capture_parec(source, out_q, stop):
    import numpy as np
    proc = subprocess.Popen(
        ["parec", f"--device={source}", "--format=s16le", f"--rate={SR}", "--channels=1",
         "--raw", "--latency-msec=50"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    nbytes = CHUNK * 2
    try:
        while not stop.is_set():
            data = proc.stdout.read(nbytes)
            if not data:
                break
            out_q.put(np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0)
    finally:
        proc.terminate()


class Resampler:
    """Streaming mono resampler to 16 kHz: box low-pass + linear interpolation, phase kept across blocks."""

    def __init__(self, rate):
        import numpy as np
        self.np = np
        self.step = rate / SR                                  # input samples per output sample
        width = max(1, int(round(self.step)))                  # low-pass so downsampling doesn't alias
        self.kernel = np.full(width, 1.0 / width, np.float32)
        self.raw = np.zeros(width - 1, dtype=np.float32)       # unfiltered history for the filter
        self.tail = np.zeros(0, dtype=np.float32)              # filtered input not yet consumed
        self.pos = 0.0                                         # next output position within tail

    def __call__(self, x):
        np = self.np
        joined = np.concatenate([self.raw, x])
        if len(self.raw):
            self.raw = joined[-len(self.raw):]
        buf = np.concatenate([self.tail, np.convolve(joined, self.kernel, "valid").astype(np.float32)])
        n = int((len(buf) - 1 - self.pos) / self.step) + 1 if len(buf) - 1 >= self.pos else 0
        out = np.interp(self.pos + self.step * np.arange(n), np.arange(len(buf)), buf).astype(np.float32)
        nxt = self.pos + self.step * n
        keep = min(int(nxt), len(buf))
        self.tail, self.pos = buf[keep:], nxt - keep
        return out


def _capture_sounddevice(source, out_q, stop):
    import numpy as np
    import sounddevice as sd
    devs, default = _wasapi_devices() if WINDOWS else (list(enumerate(sd.query_devices())), sd.default.device[0])
    idx = next((i for i, d in devs if d["name"] == source), default)
    dev = sd.query_devices(idx)
    rate, channels = int(dev["default_samplerate"]), min(2, dev["max_input_channels"])
    raw = queue.Queue()
    resample = Resampler(rate)
    pending = np.zeros(0, dtype=np.float32)
    with sd.InputStream(device=idx, samplerate=rate, channels=channels, dtype="float32",
                        blocksize=int(rate * CHUNK / SR), callback=lambda d, *_: raw.put(d.copy())):
        print(f"{APP}: capturing '{dev['name']}' at {rate} Hz", file=sys.stderr, flush=True)
        while not stop.is_set():
            try:
                block = raw.get(timeout=0.5)
            except queue.Empty:
                continue
            pending = np.concatenate([pending, resample(block.mean(axis=1))])
            while len(pending) >= CHUNK:
                out_q.put(pending[:CHUNK])
                pending = pending[CHUNK:]


# ---------------------------------------------------------------- translation

# Whisper language code -> NLLB-200 (FLORES) code. Whisper languages NLLB lacks: la, br, haw.
NLLB_CODES = {
    "en": "eng_Latn", "zh": "zho_Hans", "de": "deu_Latn", "es": "spa_Latn", "ru": "rus_Cyrl",
    "ko": "kor_Hang", "fr": "fra_Latn", "ja": "jpn_Jpan", "pt": "por_Latn", "tr": "tur_Latn",
    "pl": "pol_Latn", "ca": "cat_Latn", "nl": "nld_Latn", "ar": "arb_Arab", "sv": "swe_Latn",
    "it": "ita_Latn", "id": "ind_Latn", "hi": "hin_Deva", "fi": "fin_Latn", "vi": "vie_Latn",
    "he": "heb_Hebr", "uk": "ukr_Cyrl", "el": "ell_Grek", "ms": "zsm_Latn", "cs": "ces_Latn",
    "ro": "ron_Latn", "da": "dan_Latn", "hu": "hun_Latn", "ta": "tam_Taml", "no": "nob_Latn",
    "th": "tha_Thai", "ur": "urd_Arab", "hr": "hrv_Latn", "bg": "bul_Cyrl", "lt": "lit_Latn",
    "mi": "mri_Latn", "ml": "mal_Mlym", "cy": "cym_Latn", "sk": "slk_Latn", "te": "tel_Telu",
    "fa": "pes_Arab", "lv": "lvs_Latn", "bn": "ben_Beng", "sr": "srp_Cyrl", "az": "azj_Latn",
    "sl": "slv_Latn", "kn": "kan_Knda", "et": "est_Latn", "mk": "mkd_Cyrl", "eu": "eus_Latn",
    "is": "isl_Latn", "hy": "hye_Armn", "ne": "npi_Deva", "mn": "khk_Cyrl", "bs": "bos_Latn",
    "kk": "kaz_Cyrl", "sq": "als_Latn", "sw": "swh_Latn", "gl": "glg_Latn", "mr": "mar_Deva",
    "pa": "pan_Guru", "si": "sin_Sinh", "km": "khm_Khmr", "sn": "sna_Latn", "yo": "yor_Latn",
    "so": "som_Latn", "af": "afr_Latn", "oc": "oci_Latn", "ka": "kat_Geor", "be": "bel_Cyrl",
    "tg": "tgk_Cyrl", "sd": "snd_Arab", "gu": "guj_Gujr", "am": "amh_Ethi", "yi": "ydd_Hebr",
    "lo": "lao_Laoo", "uz": "uzn_Latn", "fo": "fao_Latn", "ht": "hat_Latn", "ps": "pbt_Arab",
    "tk": "tuk_Latn", "nn": "nno_Latn", "mt": "mlt_Latn", "sa": "san_Deva", "lb": "ltz_Latn",
    "my": "mya_Mymr", "bo": "bod_Tibt", "tl": "tgl_Latn", "mg": "plt_Latn", "as": "asm_Beng",
    "tt": "tat_Cyrl", "ln": "lin_Latn", "ha": "hau_Latn", "ba": "bak_Cyrl", "jw": "jav_Latn",
    "su": "sun_Latn", "yue": "yue_Hant",
}

# Whisper's spoken-language codes with display names.
LANGUAGE_NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani", "ba": "Bashkir",
    "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bo": "Tibetan", "br": "Breton", "bs": "Bosnian",
    "ca": "Catalan", "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German", "el": "Greek", "en": "English",
    "es": "Spanish", "et": "Estonian", "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fo": "Faroese",
    "fr": "French", "gl": "Galician", "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiian", "he": "Hebrew",
    "hi": "Hindi", "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian", "hy": "Armenian",
    "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese", "jw": "Javanese", "ka": "Georgian",
    "kk": "Kazakh", "km": "Khmer", "kn": "Kannada", "ko": "Korean", "la": "Latin", "lb": "Luxembourgish",
    "ln": "Lingala", "lo": "Lao", "lt": "Lithuanian", "lv": "Latvian", "mg": "Malagasy", "mi": "Maori",
    "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay", "mt": "Maltese",
    "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian Nynorsk", "no": "Norwegian", "oc": "Occitan",
    "pa": "Punjabi", "pl": "Polish", "ps": "Pashto", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian", "sn": "Shona",
    "so": "Somali", "sq": "Albanian", "sr": "Serbian", "su": "Sundanese", "sv": "Swedish", "sw": "Swahili",
    "ta": "Tamil", "te": "Telugu", "tg": "Tajik", "th": "Thai", "tk": "Turkmen", "tl": "Tagalog", "tr": "Turkish",
    "tt": "Tatar", "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese", "yi": "Yiddish",
    "yo": "Yoruba", "yue": "Cantonese", "zh": "Chinese",
}
# Shown as checkboxes in OBS; anything else goes in the "other languages" field.
COMMON_LANGS = ["en", "sk", "cs", "pl", "hu", "de", "fr", "es", "it", "pt", "nl",
                "ru", "uk", "tr", "ar", "hi", "ja", "ko", "zh"]
# Whisper codes M2M-100 can translate (M2M uses "jv" for Javanese and has no separate Nynorsk).
M2M_ALIASES = {"jw": "jv", "nn": "no"}
M2M_LANGS = {
    "af", "am", "ar", "az", "ba", "be", "bg", "bn", "br", "bs", "ca", "cs", "cy", "da", "de", "el", "en", "es",
    "et", "fa", "fi", "fr", "gl", "gu", "ha", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja", "jw",
    "ka", "kk", "km", "kn", "ko", "lb", "ln", "lo", "lt", "lv", "mg", "mk", "ml", "mn", "mr", "ms", "my", "ne",
    "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru", "sd", "si", "sk", "sl", "so", "sq", "sr", "su",
    "sv", "sw", "ta", "th", "tl", "tr", "uk", "ur", "uz", "vi", "yi", "yo", "zh",
}
TARGETS = set(NLLB_CODES) | M2M_LANGS                  # languages you can translate into (with some model)


class Translator:
    """Local translation on CTranslate2 (M2M-100 or NLLB-200). One worker; batches pending jobs and skips
    superseded partials. `settings.target` is read per job, so the output language can change while running."""

    def __init__(self, settings, emit):
        import ctranslate2
        from huggingface_hub import snapshot_download
        repo, revision = MT_MODELS[settings.mt_model]
        path = snapshot_download(repo, revision=revision)
        if settings.mt_model.startswith("nllb"):
            from tokenizers import Tokenizer
            tok = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))
            self.encode = lambda text: tok.encode(text, add_special_tokens=False).tokens
            self.decode = lambda pieces: tok.decode([i for i in map(tok.token_to_id, pieces) if i is not None])
            self.code = NLLB_CODES.get
        else:
            import sentencepiece
            sp = sentencepiece.SentencePieceProcessor(model_file=os.path.join(path, "sentencepiece.bpe.model"))
            with open(os.path.join(path, "shared_vocabulary.json"), encoding="utf-8") as f:
                vocab = set(json.load(f))
            self.encode = lambda text: sp.encode(text, out_type=str)
            self.decode = sp.decode
            self.code = lambda lang: (lambda t: t if t in vocab else None)(f"__{M2M_ALIASES.get(lang, lang)}__")
        self.model = ctranslate2.Translator(
            path, device=settings.device, compute_type="int8_float16" if settings.device == "cuda" else "int8")
        self.settings = settings
        self.emit = emit
        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def warmup(self):
        """One throwaway translation so the first real line isn't slowed by GPU kernel setup."""
        self.model.translate_batch([[self.code("en")] + self.encode("Hello, how are you today?") + ["</s>"]],
                                   target_prefix=[[self.code("de")]], beam_size=4)

    def submit(self, utt, rev, lang, text):
        target = self.settings.target
        if lang == target:
            self.emit(("tr", utt, rev, text))
        elif self.code(lang) is None or self.code(target) is None:
            missing = lang if self.code(lang) is None else target
            self.emit(("tr", utt, rev, f"[{self.settings.mt_model} can't translate '{missing}'] {text}"))
        else:
            self.jobs.put((utt, rev, lang, text))

    def _worker(self):
        while True:
            latest = {}
            job = self.jobs.get()
            while job is not None:
                if job[0] not in latest or job[1] > latest[job[0]][1]:
                    latest[job[0]] = job
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    job = None
            batch = list(latest.values())
            tgt = self.code(self.settings.target)
            if tgt is None:
                continue
            sources = [[self.code(lang)] + self.encode(text) + ["</s>"] for _, _, lang, text in batch]
            results = self.model.translate_batch(
                sources, target_prefix=[[tgt]] * len(batch), beam_size=4, max_decoding_length=256)
            for (utt, rev, _, _), res in zip(batch, results):
                self.emit(("tr", utt, rev, self.decode(res.hypotheses[0][1:])))


# ---------------------------------------------------------------- speech recognition

class Recognizer:
    def __init__(self, args, emit):
        from faster_whisper import WhisperModel
        from faster_whisper.vad import get_vad_model
        self.args = args
        self.emit = emit
        self.model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
        self.vad = get_vad_model()
        self.lang_scores = {}           # EMA of language probabilities (sticky language ID)

    def warmup(self):
        """Run every decode path once on silence so the first real sentence is as fast as the rest."""
        import numpy as np
        silence = np.zeros(2 * SR, dtype=np.float32)
        self.model.detect_language(silence)
        self.transcribe(silence, "en", beam=1)
        self.transcribe(silence, "en", beam=5)

    def pick_language(self, audio):
        """Spoken language for this clip, or None if it isn't one we're listening for."""
        _, _, probs = self.model.detect_language(audio)
        new = dict(probs)
        if self.args.listen:
            best = max(self.args.listen, key=lambda l: new.get(l, 0.0))
            return best if new.get(best, 0.0) >= self.args.listen_min_prob else None
        # hysteresis so short clips don't flip the language back and forth
        a = 0.6
        keys = set(new) | set(self.lang_scores)
        self.lang_scores = {k: (1 - a) * self.lang_scores.get(k, 0.0) + a * new.get(k, 0.0) for k in keys}
        return max(self.lang_scores, key=self.lang_scores.get)

    def transcribe(self, audio, lang, beam):
        """beam=1: quick live partial (single greedy pass); beam=5: the final, more careful pass.
        Output length is capped by the audio length: a decode stuck repeating itself ("all all all...")
        can otherwise run to the 448-token limit and retry at every fallback temperature, stalling for seconds."""
        segs, _ = self.model.transcribe(
            audio, language=lang, task="transcribe", beam_size=beam,
            condition_on_previous_text=False, without_timestamps=True, vad_filter=False,
            hotwords=self.args.hotwords or None,      # names/jargon Whisper should prefer
            temperature=0.0 if beam == 1 else [0.0, 0.3, 0.6],
            max_new_tokens=int(len(audio) / SR * 15) + 20,   # fast speech is ~10 tokens/s
        )
        parts = [s.text.strip() for s in segs
                 if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0) and s.compression_ratio <= 2.4]
        text = " ".join(p for p in parts if p)
        words = re.sub(r"[^\w\s]", " ", text).lower().split()
        if not words or " ".join(words) in HALLUCINATIONS:
            return ""
        if len(words) >= 4 and len(set(words)) <= 0.4 * len(words):   # "presented by presented by ..."
            return ""
        return text

    def run(self, audio_q, translator):
        import numpy as np
        hist = np.zeros(0, dtype=np.float32)          # rolling buffer for VAD + pre-roll
        buf = []                                       # current utterance chunks
        in_speech = False
        silence = 0.0
        utt = 0
        rev = 0
        last_partial = 0.0
        last_text = ""
        utt_lang = None                                # None = not detected yet, "" = not a listened-for language
        max_samples = int(self.args.max_utterance * SR)

        def finalize():
            nonlocal buf, in_speech, silence, rev, last_text, utt, utt_lang
            audio = np.concatenate(buf)
            buf, in_speech, silence = [], False, 0.0
            if len(audio) >= int(0.5 * SR):
                lang = self.pick_language(audio)
                text = self.transcribe(audio, lang, beam=5) if lang else ""
                if text:
                    rev += 1
                    self.emit(("final", utt, rev, lang, text))
                    translator.submit(utt, rev, lang, text)
                elif last_text:
                    self.emit(("drop", utt))
            elif last_text:
                self.emit(("drop", utt))
            utt += 1
            last_text, utt_lang = "", None

        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            chunks = [chunk]
            while True:                               # drain backlog so we never fall behind
                try:
                    c = audio_q.get_nowait()
                except queue.Empty:
                    break
                if c is None:
                    audio_q.put(None)
                    break
                chunks.append(c)

            for chunk in chunks:
                hist = np.concatenate([hist, chunk])[-(VAD_CONTEXT + CHUNK):]
                usable = len(hist) - len(hist) % FRAME
                probs = self.vad(hist[len(hist) - usable:]).reshape(-1)[-(len(chunk) // FRAME):]
                speech = float(probs.max()) > self.args.vad_threshold
                if not in_speech:
                    if speech:
                        in_speech = True
                        buf = [hist[-(PRE_ROLL + len(chunk)):].copy()]
                        silence = 0.0
                        last_partial = time.monotonic()
                    continue
                buf.append(chunk)
                silence = 0.0 if speech else silence + len(chunk) / SR
                total = sum(len(b) for b in buf)
                if silence >= self.args.silence or total >= max_samples:
                    finalize()

            # live partial hypothesis while someone is still talking (skipped if we're behind)
            now = time.monotonic()
            if in_speech and len(chunks) <= 2 and now - last_partial >= self.args.partial_every:
                audio = np.concatenate(buf)
                if len(audio) >= SR:
                    last_partial = now
                    if utt_lang is None:
                        utt_lang = self.pick_language(audio) or ""
                    text = self.transcribe(audio, utt_lang, beam=1) if utt_lang else ""
                    if text and text != last_text:
                        last_text = text
                        rev += 1
                        self.emit(("partial", utt, rev, utt_lang, text))
                        if self.args.translate_partials:
                            translator.submit(utt, rev, utt_lang, text)
        if buf:
            finalize()


# ---------------------------------------------------------------- output

class State:
    """Latest original/translation per utterance; ignores out-of-order (stale) results."""

    def __init__(self, keep):
        self.lines = collections.OrderedDict()   # utt -> dict(lang, text, tr, rt=text rev, rtr=translation rev, final)
        self.keep = keep

    def apply(self, ev):
        kind, utt = ev[0], ev[1]
        if kind == "drop":
            self.lines.pop(utt, None)
            return
        line = self.lines.setdefault(utt, dict(lang="", text="", tr="", rt=0, rtr=0, final=False))
        if kind in ("partial", "final"):
            _, _, rev, lang, text = ev
            if rev >= line["rt"]:
                line.update(lang=lang, text=text, rt=rev, final=kind == "final")
        elif kind == "tr":
            _, _, rev, tr = ev
            if rev >= line["rtr"]:
                line.update(tr=tr, rtr=rev)
        while len(self.lines) > self.keep:
            self.lines.popitem(last=False)


# ---------------------------------------------------------------- OBS (--serve)
#
# SullyTranslate.lua and the engine share a folder ("state"):
#   OBS writes   settings.json  {"kind": <text source id>, "settings": {...}}   whenever settings change
#                heartbeat      unix time every second, or "stop" when the script unloads
#   engine writes caption.json  {"seq", "tr", "orig", "status"}                what the sources show
#                style.json     {"kind", "tr", "orig"}                          text-source settings
#                meta.json      microphones and languages for the settings UI
#                engine.log

ENGINE_KEYS = ("mic", "model", "device", "mt", "restart")     # changing these restarts the engine
OBS_DEFAULTS = {
    "mic": "", "model": "auto", "device": "auto", "mt": "auto", "restart": 0,
    "other_langs": "", "target": "en", "hotwords": "",
    "hold": 4.0, "partials": True, "show_original": False, "width": 1600,
    "font": {"face": "Arial" if WINDOWS else "Sans Serif", "size": 48, "style": "Bold", "flags": 1},
    "color": 0xFFFFFFFF, "gradient": False, "color2": 0xFF66D9FF,
    "outline": True, "outline_size": 4, "outline_color": 0xFF000000, "shadow": True,
    "bg": False, "bg_color": 0xFF000000, "bg_opacity": 60, "align": "center",
    "orig_size": 30, "orig_color": 0xFFD0D0D0,
}


class CaptionFeed:
    """Engine events -> what the two caption sources show: live partials, finished lines, hold-time clearing."""

    def __init__(self):
        self.state = State(keep=10)
        self.newest = -1
        self.clear_at = None
        self.tr = self.orig = ""

    def _show(self, tr, orig, opts):
        self.tr, self.orig = tr, orig if opts["show_original"] else ""
        self.clear_at = None

    def feed(self, ev, opts, now):
        self.state.apply(ev)
        utt = ev[1]
        if ev[0] == "drop":
            if utt == self.newest:                     # the line being shown live turned out to be noise
                self.tr = self.orig = ""
                self.clear_at = None
            return
        ln = self.state.lines.get(utt)
        if ln is None or utt < self.newest:
            return                                     # an older line finishing late
        self.newest = utt
        same = ln["lang"] == opts["target"]
        orig = "" if same else ln["text"]
        if ln["final"] and ln["rtr"] == ln["rt"]:
            self._show(ln["tr"], orig, opts)
            if opts["hold"] > 0:
                self.clear_at = now + opts["hold"]
        elif opts["show_partials"]:
            self._show((ln["text"] if same else ln["tr"]) or self.tr, orig, opts)

    def tick(self, now):
        if self.clear_at is not None and now >= self.clear_at:
            self.tr = self.orig = ""
            self.clear_at = None


def text_style(kind, s, original):
    """OBS settings for the translation or original text source (GDI+ on Windows, FreeType 2 on Linux)."""
    font = dict(s["font"])
    if original:
        font["size"] = int(s["orig_size"])
    color = s["orig_color"] if original else s["color"]
    gradient = bool(s["gradient"]) and not original
    width = int(s["width"])
    if kind.startswith("text_gdiplus"):
        return {
            "font": font, "color": color, "opacity": 100, "antialiasing": True,
            "gradient": gradient, "gradient_color": s["color2"], "gradient_opacity": 100, "gradient_dir": 90.0,
            "outline": bool(s["outline"]), "outline_size": int(s["outline_size"]),
            "outline_color": s["outline_color"], "outline_opacity": 100,
            "bk_color": s["bg_color"], "bk_opacity": int(s["bg_opacity"]) if s["bg"] else 0,
            "align": s["align"], "valign": "bottom",
            "extents": width > 0, "extents_cx": width, "extents_wrap": width > 0,
            "extents_cy": int(font.get("size", 48) * 1.4 * (2 if original else 3)),
        }
    return {
        "font": font, "color1": color, "color2": s["color2"] if gradient else color,
        "outline": bool(s["outline"]), "drop_shadow": bool(s["shadow"]),
        "word_wrap": width > 0, "custom_width": width, "antialiasing": True,
    }


def caption_options(args, s):
    """Apply the live (no-restart) OBS settings to the engine; returns the CaptionFeed options."""
    by_name = {n.lower(): c for c, n in LANGUAGE_NAMES.items()}
    listen = [c for c in COMMON_LANGS if s.get("lang_" + c)]
    for word in re.split(r"[,;\n]+", str(s.get("other_langs", "")).lower()):
        code = word.strip() if word.strip() in LANGUAGE_NAMES else by_name.get(word.strip())
        if code and code not in listen:
            listen.append(code)
    args.listen = listen or None
    if s.get("target") in TARGETS:
        args.target = s["target"]
    args.hotwords = str(s.get("hotwords", "")).strip() or None
    return {"target": args.target, "hold": float(s["hold"]), "show_partials": bool(s["partials"]),
            "show_original": bool(s["show_original"])}


def write_meta(path):
    """Microphones and languages for the OBS settings UI."""
    names = lambda codes: [{"code": c, "name": LANGUAGE_NAMES.get(c, c)} for c in codes]
    meta = {
        "inputs": list_inputs(), "default_input": default_input(),
        "common": names(COMMON_LANGS),
        "targets": [{"code": c, "name": LANGUAGE_NAMES.get(c, c) + ("" if c in M2M_LANGS else " (NLLB only)")}
                    for c in sorted(TARGETS, key=lambda c: LANGUAGE_NAMES.get(c, c))],
    }
    _write_json(path, meta)
    return meta


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)                              # ASCII-escaped, so OBS's parser never sees raw UTF-8 issues
    for _ in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                        # Windows: OBS has it open for a moment
            time.sleep(0.02)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _single_instance(path, wait=15.0):
    """Exclusive lock on `path`; waits for a previous engine to exit. Returns the open file or None."""
    f = open(path, "a+")
    deadline = time.monotonic() + wait
    while True:
        try:
            if WINDOWS:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() > deadline:
                f.close()
                return None
            time.sleep(0.25)


def _respawn():
    """Start a fresh engine (new mic/model) and exit; the new one takes over once our lock is gone."""
    flags = 0x08000000 | 0x00000008 if WINDOWS else 0  # CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen([sys.executable] + sys.argv, creationflags=flags, start_new_session=not WINDOWS,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os._exit(0)


def serve(args):
    d = args.serve
    os.makedirs(d, exist_ok=True)
    p = lambda name: os.path.join(d, name)
    log = open(p("engine.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log                      # pythonw on Windows has no console at all
    instance_lock = _single_instance(p("engine.lock"))   # held (referenced) for the engine's lifetime
    if instance_lock is None:
        print(f"{APP}: another engine is already serving {d}; exiting")
        return
    log.truncate(0)
    print(f"{APP} engine started {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()}")

    caption = {"seq": 0, "tr": "", "orig": "", "status": ""}
    lock = threading.Lock()

    def publish(**kw):
        with lock:
            caption.update(kw)
            caption["seq"] += 1
            _write_json(p("caption.json"), caption)

    def load_settings():
        raw = _read_json(p("settings.json")) or {}
        s = dict(OBS_DEFAULTS)
        s.update(raw.get("settings") or {})
        return raw.get("kind") or ("text_gdiplus_v3" if WINDOWS else "text_ft2_source_v2"), s

    publish(status="starting…")
    meta = write_meta(p("meta.json"))
    kind, s = load_settings()
    _write_json(p("style.json"), {"kind": kind, "tr": text_style(kind, s, False), "orig": text_style(kind, s, True)})
    engine_key = tuple(s[k] for k in ENGINE_KEYS)
    opts = caption_options(args, s)
    started = time.time()

    def control():
        """Live settings, style, heartbeat, microphone list: runs even while models load."""
        nonlocal opts, meta
        seen, last_meta, last_alive = None, time.monotonic(), 0.0
        while True:
            time.sleep(0.2)
            if time.monotonic() - last_alive > 2:          # lets the OBS script restart a crashed engine
                last_alive = time.monotonic()
                with open(p("alive"), "w") as f:
                    f.write(str(int(time.time())))
            try:
                hb_text = open(p("heartbeat")).read().strip()
                hb_age = time.time() - os.path.getmtime(p("heartbeat"))
            except OSError:
                hb_text, hb_age = "", time.time() - started
            if hb_text == "stop" or (hb_age > 10 and time.time() - started > 15):
                print(f"{APP}: OBS is gone ({'script unloaded' if hb_text == 'stop' else 'no heartbeat'}); exiting")
                os._exit(0)
            try:
                m = os.path.getmtime(p("settings.json"))
            except OSError:
                m = None
            if m != seen:
                seen = m
                new_kind, new = load_settings()
                if tuple(new[k] for k in ENGINE_KEYS) != engine_key:
                    print(f"{APP}: microphone/model/device changed; restarting engine")
                    publish(status="restarting engine…")
                    _respawn()
                _write_json(p("style.json"), {"kind": new_kind, "tr": text_style(new_kind, new, False),
                                              "orig": text_style(new_kind, new, True)})
                opts = caption_options(args, new)
            if time.monotonic() - last_meta > 5:
                last_meta = time.monotonic()
                fresh = {"inputs": list_inputs(), "default_input": default_input()}
                if {k: meta[k] for k in fresh} != fresh:
                    meta = write_meta(p("meta.json"))

    threading.Thread(target=control, daemon=True).start()

    ids = {i["id"]: i["name"] for i in meta["inputs"]}
    source = s["mic"] if s["mic"] in ids else meta["default_input"]
    args.model, args.device = s["model"], s["device"]
    args.mt_model = s["mt"] if s["mt"] in MT_MODELS else "auto"
    args.compute_type = "auto"
    try:
        resolve_hardware(args)
        publish(status=f"loading models ({args.model} on {'GPU' if args.device == 'cuda' else 'CPU'}); "
                       "the first start downloads several GB…")
        ev_q = queue.Queue()
        try:
            rec, tr = Recognizer(args, ev_q.put), Translator(args, ev_q.put)
        except Exception as e:
            if args.device != "cuda":
                raise
            print(f"{APP}: GPU start failed ({e}); falling back to CPU")
            args.device, args.compute_type = "cpu", "int8"
            args.model = "large-v3-turbo" if s["model"] == "auto" else s["model"]
            args.mt_model = "m2m-small" if s["mt"] == "auto" else s["mt"]
            resolve_hardware(args)
            rec, tr = Recognizer(args, ev_q.put), Translator(args, ev_q.put)
    except Exception as e:
        print(f"{APP}: engine failed to start: {e!r}")
        publish(status=f"error: {e} (see engine.log)")
        threading.Event().wait()                       # keep answering the heartbeat so OBS can show the error

    publish(status="warming up…")
    rec.warmup()
    tr.warmup()
    audio_q, stop = queue.Queue(), threading.Event()
    threading.Thread(target=capture, args=(source, audio_q, stop), daemon=True).start()
    worker = threading.Thread(target=rec.run, args=(audio_q, tr), daemon=True)
    worker.start()
    hw = "GPU" if args.device == "cuda" else "CPU"
    publish(status=f"listening: {ids.get(source, source)} · {args.model} on {hw}")
    print(f"{APP}: ready; source {source}, model {args.model}, translation {args.mt_model}, {hw}")

    feed = CaptionFeed()
    while True:
        now = time.monotonic()
        try:
            while True:
                feed.feed(ev_q.get(timeout=0.05), opts, now)
        except queue.Empty:
            pass
        feed.tick(time.monotonic())
        if (feed.tr, feed.orig) != (caption["tr"], caption["orig"]):
            publish(tr=feed.tr, orig=feed.orig)
        if not worker.is_alive() and "error" not in caption["status"]:
            publish(status="error: the audio input stopped (see engine.log); pick another microphone")


def run_cli(ev_q, stop):
    state = State(keep=50)
    printed = set()
    while not stop.is_set():
        try:
            ev = ev_q.get(timeout=0.2)
        except queue.Empty:
            continue
        state.apply(ev)
        line = state.lines.get(ev[1])
        if line and line["final"] and ev[0] == "tr" and ev[1] not in printed:
            printed.add(ev[1])
            print(f"[{line['lang']}] {line['text']}\n  -> {line['tr']}", flush=True)


def monitor_geometry(root, want):
    """(x, y, w, h) of the chosen monitor (index or name, default primary); Tk only sees the virtual screen."""
    try:
        out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        out = ""
    mons = [(m[1], m[3], m[2] == "*", tuple(int(m[i]) for i in (6, 7, 4, 5)))
            for m in re.finditer(r"^\s*(\d+):\s+\+?(\*?)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", out, re.M)]
    for idx, name, primary, geo in mons:
        if want in (idx, name) or (want is None and primary):
            return geo
    if mons:
        return mons[0][3]
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def run_gui(ev_q, stop, args):
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk

    bg, dim, line = "black", "#a0a0a0", "#333333"
    root = tk.Tk()
    root.withdraw()
    root.title(APP)
    if root.tk.call("tk", "windowingsystem") == "x11":
        # A WM-managed "dock" window: undecorated, never takes focus, and KWin/GNOME keep it above
        # native Wayland windows (an override-redirect window gets buried under them).
        root.attributes("-type", "dock")
    else:
        root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", args.opacity)
    root.configure(bg=bg)
    mx, my, mw, mh = monitor_geometry(root, args.monitor)
    w, h = int(mw * 0.8), int(args.font_size * 10)
    root.geometry(f"{w}x{h}+{mx + (mw - w) // 2}+{my + mh - h - 80}")

    big = tkfont.Font(family="sans-serif", size=args.font_size, weight="bold")
    small = tkfont.Font(family="sans-serif", size=max(9, int(args.font_size * 0.6)))
    hist_meta = tkfont.Font(family="sans-serif", size=max(8, int(args.font_size * 0.4)))
    hist_tr = tkfont.Font(family="sans-serif", size=max(9, int(args.font_size * 0.5)))

    root.columnconfigure(0, weight=3, uniform="pane")          # live : history = 3 : 2
    root.columnconfigure(2, weight=2, uniform="pane")
    root.rowconfigure(0, weight=1)

    # --- left: live subtitle
    live = tk.Frame(root, bg=bg, padx=16, pady=10)
    live.grid(row=0, column=0, sticky="nsew")
    orig = tk.Label(live, bg=bg, fg=dim, font=small, justify="center")
    trans = tk.Label(live, bg=bg, fg="white", font=big, justify="center")
    orig.pack(fill="x")
    trans.pack(fill="both", expand=True)
    live.bind("<Configure>", lambda e: [lbl.config(wraplength=max(50, e.width - 32)) for lbl in (orig, trans)])

    tk.Frame(root, bg=line, width=1).grid(row=0, column=1, sticky="ns")

    # --- right: scrollable history of finished lines
    side = tk.Frame(root, bg=bg)
    side.grid(row=0, column=2, sticky="nsew")
    header = tk.Frame(side, bg=bg)
    header.pack(fill="x")
    title = tk.Label(header, text="History", bg=bg, fg=dim, font=hist_meta, padx=8, pady=3)
    title.pack(side="left")
    close = tk.Label(header, text="✕", bg=bg, fg=dim, font=hist_meta, padx=8, pady=3, cursor="hand2")
    close.pack(side="right")
    close.bind("<Button-1>", lambda e: stop.set())
    body = tk.Frame(side, bg=bg)
    body.pack(fill="both", expand=True)
    history = tk.Text(body, bg=bg, fg="white", wrap="word", bd=0, highlightthickness=0, padx=8, pady=4,
                      cursor="arrow", selectbackground="#444444", inactiveselectbackground="#444444")
    bar = tk.Scrollbar(body, command=history.yview, width=8, bd=0, highlightthickness=0,
                       bg=line, troughcolor=bg, activebackground="#666666", elementborderwidth=0)
    history.configure(yscrollcommand=bar.set)
    bar.pack(side="right", fill="y")
    history.pack(side="left", fill="both", expand=True)
    history.tag_configure("meta", font=hist_meta, foreground="#808080", spacing1=8)
    history.tag_configure("tr", font=hist_tr, foreground="white", spacing3=2)
    history.configure(state="disabled")
    style = ttk.Style(root)
    style.configure("Dark.TSizegrip", background=bg)
    ttk.Sizegrip(side, style="Dark.TSizegrip").place(relx=1.0, rely=1.0, anchor="se")

    state = State(keep=10)
    show_orig = [not args.hide_original]
    logged = set()                                   # utterances already in the history pane

    def add_history(ln):
        at_bottom = history.yview()[1] >= 0.999     # only follow new lines if not scrolled up
        top = int(history.index("@0,0").split(".")[0])
        history.configure(state="normal")
        said = "" if ln["lang"] == args.target else f" {ln['text']}"   # already in your language: show once
        history.insert("end", f"{time.strftime('%H:%M:%S')}  [{ln['lang']}]{said}\n", "meta")
        history.insert("end", f"{ln['tr']}\n", "tr")
        trimmed = int(history.index("end-1c").split(".")[0]) - 1 > 2 * args.history
        if trimmed:
            history.delete("1.0", "3.0")              # each entry is exactly two logical lines
        history.configure(state="disabled")
        if at_bottom:
            history.see("end")
        elif trimmed:
            history.yview(f"{max(1, top - 2)}.0")      # keep what you're reading in place

    def render():
        if not state.lines:
            orig.config(text="")
            trans.config(text="listening…")
            return
        items = list(state.lines.values())
        cur = items[-1]
        if not show_orig[0]:
            o = ""
        elif cur["lang"] == args.target:
            o = f"[{cur['lang']}]"                    # already in your language: no duplicate line
        else:
            o = f"[{cur['lang']}] {cur['text']}"
        if cur["lang"] == args.target and not cur["tr"]:
            cur = dict(cur, tr=cur["text"])            # --no-translate-partials: nothing to wait for
        t = cur["tr"] or ("…" if not cur["final"] else "")
        if len(items) > 1 and items[-2]["tr"] and not cur["tr"]:
            t = items[-2]["tr"] + "\n…"            # keep previous translation visible while next arrives
        orig.config(text=o)
        trans.config(text=t, fg="white" if cur["final"] else "#dddddd")

    def poll():
        changed = False
        while True:
            try:
                ev = ev_q.get_nowait()
            except queue.Empty:
                break
            state.apply(ev)
            changed = True
            ln = state.lines.get(ev[1])
            # the final line's own translation has the same revision as its final text
            if ev[0] == "tr" and ln and ln["final"] and ln["rtr"] == ln["rt"] and ev[1] not in logged:
                logged.add(ev[1])
                add_history(ln)
        if changed:
            render()
        if stop.is_set():
            root.destroy()
            return
        root.after(50, poll)

    # drag the window by the live pane or the history header; offsets are window-relative
    drag = {}
    def start_drag(e):
        drag.update(x=e.x_root - root.winfo_x(), y=e.y_root - root.winfo_y())
    def do_drag(e):
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
    def resize(delta):
        big.configure(size=max(10, big.cget("size") + delta))
        small.configure(size=max(8, int(big.cget("size") * 0.6)))
    def toggle_orig(e):
        show_orig[0] = not show_orig[0]
        render()
    for wdg in (live, orig, trans, header, title):
        wdg.bind("<ButtonPress-1>", start_drag)
        wdg.bind("<B1-Motion>", do_drag)
    for wdg in (live, orig, trans):
        wdg.bind("<Double-Button-1>", toggle_orig)
        wdg.bind("<Button-4>", lambda e: resize(2))
        wdg.bind("<Button-5>", lambda e: resize(-2))
        wdg.bind("<MouseWheel>", lambda e: resize(2 if e.delta > 0 else -2))
        wdg.bind("<Button-3>", lambda e: stop.set())
    root.bind("<Escape>", lambda e: stop.set())
    render()
    root.deiconify()
    root.attributes("-topmost", True)
    root.after(50, poll)
    root.mainloop()
    stop.set()


# ---------------------------------------------------------------- main

def main():
    sys_lang = (locale.getlocale()[0] or os.environ.get("LANG") or "en").split("_")[0].split(".")[0] or "en"
    sys_lang = sys_lang if sys_lang in TARGETS else "en"
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--serve", metavar="DIR", help="OBS mode: run the engine for SullyTranslate.lua using DIR")
    p.add_argument("--download-models", action="store_true", help="download the models 'auto' picks, then exit")
    p.add_argument("--write-meta", metavar="FILE", help="write microphones/languages for the OBS UI, then exit")
    p.add_argument("--list-inputs", action="store_true", help="print capture devices as JSON, then exit")
    p.add_argument("-t", "--target", default=sys_lang, help=f"translate into this language code (default: {sys_lang})")
    p.add_argument("-s", "--source", help="capture device id from --list-inputs "
                                          "(default: speaker monitor on Linux, default microphone on Windows)")
    p.add_argument("-l", "--listen", nargs="+", metavar="LANG",
                   help="only subtitle speech in these languages (e.g. -l ja or -l es pt); ignore everything else")
    p.add_argument("--listen-min-prob", type=float, default=0.3,
                   help="min language-ID probability for speech to count as a --listen language")
    p.add_argument("-m", "--model", default="auto",
                   help="whisper model: auto, large-v3, large-v3-turbo, medium, small (auto: large-v3 on GPU)")
    p.add_argument("--mt-model", default="auto", choices=["auto", *MT_MODELS],
                   help="translation model: auto (m2m on GPU, m2m-small on CPU), m2m / m2m-small (MIT, "
                        "commercial use OK), nllb / nllb-fast (better, but non-commercial use only)")
    p.add_argument("--device", default="auto", help="auto, cuda or cpu")
    p.add_argument("--compute-type", default="auto", help="auto, float16 (GPU), int8 (CPU)")
    p.add_argument("--hotwords", help="names/jargon to bias recognition toward, e.g. \"tofito World of Tanks\"")
    p.add_argument("--cli", action="store_true", help="print to terminal instead of overlay")
    p.add_argument("--monitor", help="monitor index or name for the overlay (default: primary; see xrandr --listmonitors)")
    p.add_argument("--font-size", type=int, default=26)
    p.add_argument("--opacity", type=float, default=0.8)
    p.add_argument("--hide-original", action="store_true", help="only show the translation")
    p.add_argument("--history", type=int, default=500, help="number of finished lines kept in the history pane")
    p.add_argument("--silence", type=float, default=0.6, help="seconds of silence that end a sentence")
    p.add_argument("--max-utterance", type=float, default=12.0, help="force a cut after this many seconds")
    p.add_argument("--partial-every", type=float, default=0.8, help="seconds between live partial updates")
    p.add_argument("--no-translate-partials", dest="translate_partials", action="store_false")
    p.add_argument("--vad-threshold", type=float, default=0.5)
    args = p.parse_args()

    if args.list_inputs:
        print(json.dumps(list_inputs(), indent=2))
        return
    if args.write_meta:
        write_meta(args.write_meta)
        return
    if args.device != "cpu":
        setup_gpu_libs()
    if args.serve:
        serve(args)
        return
    if args.download_models:
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
        from faster_whisper.utils import download_model
        from huggingface_hub import snapshot_download
        resolve_hardware(args)
        print(f"{APP}: {'NVIDIA GPU found' if args.device == 'cuda' else 'no NVIDIA GPU; using CPU'}; "
              f"downloading {args.model} + {args.mt_model}", flush=True)
        download_model(args.model)
        repo, revision = MT_MODELS[args.mt_model]
        snapshot_download(repo, revision=revision)
        return

    resolve_hardware(args)
    if args.target not in TARGETS:
        sys.exit(f"{APP}: unsupported target language '{args.target}'")
    bad = [l for l in args.listen or [] if l not in LANGUAGE_NAMES]
    if bad:
        sys.exit(f"{APP}: unknown --listen language(s) {bad}; use codes like {sorted(LANGUAGE_NAMES)[:12]}…")
    source = args.source or (default_input() if WINDOWS else default_monitor())
    if source not in {i["id"] for i in list_inputs()}:
        sys.exit(f"{APP}: no such audio input '{source}' (see --list-inputs)")
    listen = f", listening for {'/'.join(args.listen)}" if args.listen else ""
    print(f"{APP}: capturing {source}, model {args.model} on {args.device}, target '{args.target}'{listen}",
          file=sys.stderr)

    stop = threading.Event()
    audio_q, ev_q = queue.Queue(), queue.Queue()
    rec = Recognizer(args, ev_q.put)
    tr = Translator(args, ev_q.put)
    threading.Thread(target=capture, args=(source, audio_q, stop), daemon=True).start()
    threading.Thread(target=rec.run, args=(audio_q, tr), daemon=True).start()
    print(f"{APP}: ready", file=sys.stderr, flush=True)
    try:
        if args.cli:
            run_cli(ev_q, stop)
        else:
            run_gui(ev_q, stop, args)
    except KeyboardInterrupt:
        pass
    stop.set()


if __name__ == "__main__":
    main()
