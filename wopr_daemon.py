#!/usr/bin/env python3
"""
wopr_daemon.py — Asterisk AudioSocket daemon: 2600 Hz whistle → WOPR.

A caller on a coin-op payphone hears silence. Whistle 2600 Hz and the line winks (480 Hz
burst), then WOPR from *WarGames* greets Professor Falken and holds a conversation:
energy-VAD endpointing → local faster-whisper STT → local Ollama LLM → espeak-ng "+croak" TTS →
20 ms-paced playback. A second whistle at any time barges in and resets the session. Hangup
tears everything down.

Single file by design. Sections, top to bottom:
  TUNABLES → DSP → AUDIOSOCKET FRAMING → LOGGING → PERSONA → TTS → STT → LLM → DETECTOR/VAD →
  CALL SESSION → TASKS (reader/writer/brain) → SERVER / MAIN

Protocol: TCP; frame = [1 byte type][2 byte big-endian length][payload].
Audio: signed 16-bit LE, 8 kHz mono, 20 ms frames = 160 samples = 320 bytes.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import enum
import io
import json
import logging
import math
import os
import re
import resource
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid

import numpy as np

# ═══════════════════════════════════════════════════════════════════════════════════════════
# TUNABLES — everything the operator may want to tune lives here. Values with "measured" in
# the comment were set from harness runs (see README "Tuning"); the rest are design defaults.
# ═══════════════════════════════════════════════════════════════════════════════════════════

SAMPLE_RATE = 8000            # Hz; fixed by AudioSocket / Asterisk slin
FRAME_SAMPLES = 160           # 20 ms
FRAME_BYTES = FRAME_SAMPLES * 2
WINDOW_SAMPLES = 80           # 10 ms analysis window (two per frame)
WINDOW_BYTES = WINDOW_SAMPLES * 2

# --- 2600 Hz detector -----------------------------------------------------------------------
GOERTZEL_N = 80               # samples per Goertzel window → 100 Hz bins at 8 kHz
BINS_HZ = (2500, 2600, 2700)  # k = 25, 26, 27: exactly on-bin, no leakage penalty at nominal
RATIO_THRESHOLD = 0.35        # E2600 / (E2500 + E2700): ratio-based so level doesn't matter
MIN_BIN_ENERGY = 1.0e8        # absolute floor on E2600 (int16² units); rejects silence/hiss.
                              # Measured 2026-09-04 on the harness corpus: speech windows that slip
                              # past ratio+purity bottom out at 3.8e7; the quietest passing window of
                              # a −14 dBFS synthetic whistle is 7.8e9; a −30 dBFS whistle ≈ 1.6e9.
                              # 1e8 (≈ −42 dBFS at the bin) sits ≥ 2.6× above the former and ≥ 16×
                              # below the latter. Speech rejection is the purity gate's job, not this.
PURITY_MIN = 0.10             # E2600 / (N·Σx²): fraction of window energy at 2600 Hz. A pure
                              # on-bin tone scores 0.5, ±30 Hz drift ≈ 0.37, ±50 Hz ≈ 0.2. Speech
                              # and far-off tones (whose leakage passes the ratio test alone with
                              # ratio ≈ 0.5) score ≪ 0.05. Complements the ratio test.
HOLD_WINDOWS = 30             # consecutive passing windows before a hit (300 ms)
MISS_BUDGET = 2               # dropouts tolerated inside a run (human whistles falter)
REARM_WINDOWS = 10            # consecutive non-passing windows required after a hit before the
                              # detector may fire again (so one long whistle fires once)
ECHO_GUARD_WINDOWS = 20       # outbound windows (200 ms) whose "pass" verdict masks inbound
                              # windows — sidetone self-trigger guard that keeps barge-in live

# --- Wink tone ------------------------------------------------------------------------------
WINK_HZ = 480
WINK_MS = 200
WINK_AMPLITUDE = 0.5          # of full scale

# --- Energy VAD / endpointing -----------------------------------------------------------------
VAD_START_RATIO = 3.0         # speech when RMS > floor × ratio ...
VAD_ABS_MIN = 250.0           # ... and RMS > this (int16 units); placeholder until measured
VAD_START_WINDOWS = 3         # windows above threshold to declare speech start (30 ms)
VAD_PREROLL_MS = 200          # audio kept from before speech start
VAD_FLOOR_INIT_MS = 200       # windows used to seed the noise floor after capture_start
ENDPOINT_MS = 800             # silence that ends an utterance
MIN_UTTERANCE_MS = 300        # shorter utterances are discarded (clicks, breaths)
MAX_UTTERANCE_MS = 15000      # force endpoint

# --- TTS (espeak-ng, Votrax SC-01 homage — do not substitute a neural voice) -------------------
ESPEAK_VOICE = "en-us+croak"
ESPEAK_SPEED = 140
ESPEAK_PITCH = 30
ESPEAK_AMPLITUDE = 150
ESPEAK_RATE_HZ = 22050        # what espeak-ng --stdout emits

# --- LLM (Ollama, local) ----------------------------------------------------------------------
LLM_NUM_PREDICT = 80
LLM_NUM_CTX = 2048
LLM_TEMPERATURE = 0.7
LLM_STOP = ["\n\n"]
LLM_THINK = False             # qwen3 hybrid reasoning off; dropped automatically if the model rejects it
LLM_TIMEOUT_S = 20
LLM_WARMUP_RETRY_S = 2
HISTORY_MAX_TURNS = 20        # messages (user+assistant) kept
MAX_REPLY_CHARS = 280

# --- Transport --------------------------------------------------------------------------------
OUTBOUND_QUEUE_FRAMES = 1500  # 30 s of audio; producers block beyond this
FRAME_PERIOD_S = 0.020

DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 9092,
    "log_level": "INFO",
    "ollama_url": "http://127.0.0.1:11434",
    "model": "qwen3:8b",
    "whisper_model": "base.en",
}

TUNABLE_NAMES = [
    "GOERTZEL_N", "BINS_HZ", "RATIO_THRESHOLD", "MIN_BIN_ENERGY", "PURITY_MIN", "HOLD_WINDOWS",
    "MISS_BUDGET", "REARM_WINDOWS", "ECHO_GUARD_WINDOWS", "WINK_HZ", "WINK_MS", "WINK_AMPLITUDE",
    "VAD_START_RATIO", "VAD_ABS_MIN", "VAD_START_WINDOWS", "ENDPOINT_MS", "MIN_UTTERANCE_MS",
    "MAX_UTTERANCE_MS", "ESPEAK_VOICE", "ESPEAK_SPEED", "ESPEAK_PITCH", "ESPEAK_AMPLITUDE",
    "LLM_NUM_PREDICT", "LLM_NUM_CTX", "LLM_TEMPERATURE", "LLM_THINK", "LLM_TIMEOUT_S",
    "LLM_WARMUP_RETRY_S", "HISTORY_MAX_TURNS", "MAX_REPLY_CHARS", "OUTBOUND_QUEUE_FRAMES",
]


def tunables_line() -> str:
    g = globals()
    return " ".join(f"{n}={g[n]!r}".replace(" ", "") for n in TUNABLE_NAMES)


# ═══════════════════════════════════════════════════════════════════════════════════════════
# DSP — pure functions on numpy arrays. No I/O. Imported by harness.py and tests.
# ═══════════════════════════════════════════════════════════════════════════════════════════

def pcm16_to_f32(pcm: bytes | np.ndarray) -> np.ndarray:
    a = np.frombuffer(pcm, dtype="<i2") if isinstance(pcm, (bytes, bytearray, memoryview)) else pcm
    return a.astype(np.float32) / 32768.0


def f32_to_pcm16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x * 32767.0), -32768, 32767).astype("<i2")


def pcm_windows(pcm: bytes, n: int = WINDOW_SAMPLES) -> list[np.ndarray]:
    """Split raw int16 LE bytes into full n-sample windows (int16 arrays). Remainder dropped."""
    a = np.frombuffer(pcm, dtype="<i2")
    full = (len(a) // n) * n
    return [a[i:i + n] for i in range(0, full, n)]


def goertzel_energy(x: np.ndarray, k: int, n: int = GOERTZEL_N) -> float:
    """Reference scalar Goertzel: |X_k|² for bin k of an n-point DFT of x[:n]."""
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for v in x[:n]:
        s0 = float(v) + coeff * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def hz_to_bin(hz: float, n: int = GOERTZEL_N, sr: int = SAMPLE_RATE) -> int:
    return int(round(hz * n / sr))


def _dft_matrix(bins: tuple[int, ...], n: int) -> np.ndarray:
    t = np.arange(n)
    return np.stack([np.exp(-2j * math.pi * k * t / n) for k in bins], axis=1)  # (n, len(bins))


_BINS_K = tuple(hz_to_bin(h) for h in BINS_HZ)          # (25, 26, 27)
_E_MATRIX = _dft_matrix(_BINS_K, GOERTZEL_N)


def window_energies(win: np.ndarray, matrix: np.ndarray = _E_MATRIX) -> np.ndarray:
    """Vectorized Goertzel: energies for each bin column of `matrix`. Same value as goertzel_energy."""
    x = win[:matrix.shape[0]].astype(np.float64)
    return np.abs(x @ matrix) ** 2


def window_ratio(win: np.ndarray) -> tuple[float, float, float]:
    """Return (ratio, e2600, purity) for one 80-sample int16 window.

    ratio  = E2600 / (E2500 + E2700)   — the user-specified adjacent-bin test
    purity = E2600 / (N · Σx²)         — fraction of window energy at 2600 Hz (Parseval)
    """
    e = window_energies(win)
    e25, e26, e27 = float(e[0]), float(e[1]), float(e[2])
    ratio = e26 / (e25 + e27 + 1e-9)
    total = GOERTZEL_N * float(np.sum(win[:GOERTZEL_N].astype(np.float64) ** 2)) + 1e-9
    return ratio, e26, e26 / total


def window_passes(win: np.ndarray) -> tuple[bool, float, float, float]:
    ratio, e26, purity = window_ratio(win)
    ok = ratio >= RATIO_THRESHOLD and e26 >= MIN_BIN_ENERGY and purity >= PURITY_MIN
    return ok, ratio, e26, purity


def rms(win: np.ndarray) -> float:
    x = win.astype(np.float64)
    return float(math.sqrt(np.mean(x * x))) if len(x) else 0.0


def synth_tone(hz: float, ms: float, amplitude: float = 0.5, sr: int = SAMPLE_RATE,
               phase: float = 0.0) -> np.ndarray:
    """int16 sine of `ms` milliseconds at `amplitude` × full scale."""
    n = int(round(sr * ms / 1000.0))
    t = np.arange(n) / sr
    return f32_to_pcm16(amplitude * np.sin(2 * math.pi * hz * t + phase))


def fir_lowpass_taps(cutoff_hz: float, sr: int, ntaps: int = 64) -> np.ndarray:
    """Hamming-windowed sinc low-pass, unity DC gain."""
    fc = cutoff_hz / sr
    m = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = np.sinc(2 * fc * m) * 2 * fc
    h *= np.hamming(ntaps)
    return h / np.sum(h)


def resample(x: np.ndarray, src_sr: int, dst_sr: int, cutoff_hz: float | None = None) -> np.ndarray:
    """Resample a 1-D signal (int16 or float). Anti-alias FIR when downsampling; np.interp always.
    Returns the same dtype family as the input (int16 in → int16 out)."""
    if src_sr == dst_sr or len(x) == 0:
        return x.copy()
    was_int = np.issubdtype(x.dtype, np.integer)
    y = x.astype(np.float64)
    if dst_sr < src_sr:
        fc = cutoff_hz if cutoff_hz is not None else 0.45 * dst_sr
        y = np.convolve(y, fir_lowpass_taps(fc, src_sr), mode="same")
    n_dst = int(round(len(y) * dst_sr / src_sr))
    t_src = np.arange(len(y)) / src_sr
    t_dst = np.arange(n_dst) / dst_sr
    z = np.interp(t_dst, t_src, y)
    return np.clip(np.rint(z), -32768, 32767).astype("<i2") if was_int else z.astype(np.float32)


def frames_from_pcm(pcm: bytes | np.ndarray) -> list[bytes]:
    """Split int16 audio into 320-byte frames; the last frame is zero-padded."""
    b = pcm.astype("<i2").tobytes() if isinstance(pcm, np.ndarray) else bytes(pcm)
    if len(b) % 2:
        b = b[:-1]
    pad = (-len(b)) % FRAME_BYTES
    if pad:
        b += b"\x00" * pad
    return [b[i:i + FRAME_BYTES] for i in range(0, len(b), FRAME_BYTES)]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# AUDIOSOCKET FRAMING
# ═══════════════════════════════════════════════════════════════════════════════════════════

class FrameType(enum.IntEnum):
    TERMINATE = 0x00
    UUID = 0x01
    AUDIO = 0x10
    ERROR = 0xFF


def encode_frame(ftype: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large")
    return bytes((ftype,)) + len(payload).to_bytes(2, "big") + payload


def decode_frame(buf: bytes) -> tuple[int, bytes, bytes]:
    """Decode one frame from the front of buf → (type, payload, rest). Raises if incomplete."""
    if len(buf) < 3:
        raise ValueError("incomplete header")
    ftype, length = buf[0], int.from_bytes(buf[1:3], "big")
    if len(buf) < 3 + length:
        raise ValueError("incomplete payload")
    return ftype, bytes(buf[3:3 + length]), bytes(buf[3 + length:])


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr = await reader.readexactly(3)
    length = int.from_bytes(hdr[1:3], "big")
    payload = await reader.readexactly(length) if length else b""
    return hdr[0], payload


SILENCE_FRAME = encode_frame(FrameType.AUDIO, b"\x00" * FRAME_BYTES)


class CallEnded(Exception):
    """Raised inside the reader when the far end terminates or disconnects."""


class PipelineError(Exception):
    def __init__(self, stage: str, err: str):
        super().__init__(f"{stage}: {err}")
        self.stage, self.err = stage, err


# ═══════════════════════════════════════════════════════════════════════════════════════════
# LOGGING — one event per line, key=value, ISO-8601 UTC, call=<uuid8>
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        return time.strftime("%Y-%m-%dT%H:%M:%S", ct) + f".{int(record.msecs):03d}Z"


class _CallDefault(logging.Filter):
    """Third-party records have no `call` attribute; give them the server marker."""

    def filter(self, record):
        if not hasattr(record, "call"):
            record.call = "-"
        return True


def setup_logging(level: str) -> None:
    h = logging.StreamHandler(sys.stdout)
    h.addFilter(_CallDefault())
    h.setFormatter(_UTCFormatter("%(asctime)s %(levelname)s call=%(call)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # third-party noise
    for name in ("faster_whisper", "ctranslate2", "huggingface_hub", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


class CallLogger(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("call", self.extra.get("call", "-"))
        return msg, kwargs


def get_logger(call: str = "-") -> CallLogger:
    return CallLogger(logging.getLogger("wopr"), {"call": call})


def kv(**pairs) -> str:
    out = []
    for k, v in pairs.items():
        if isinstance(v, float):
            v = f"{v:.3f}" if abs(v) < 1000 else f"{v:.3g}"
        elif isinstance(v, str) and (" " in v or v == ""):
            v = json.dumps(v)
        out.append(f"{k}={v}")
    return " ".join(out)


class Stopwatch:
    """`with Stopwatch() as sw: ...; sw.ms` — elapsed milliseconds."""

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self.t0) * 1000.0
        return False

    @property
    def now_ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000.0


# ═══════════════════════════════════════════════════════════════════════════════════════════
# PERSONA — WOPR
# ═══════════════════════════════════════════════════════════════════════════════════════════

WOPR_SYSTEM_PROMPT = (
    "You are WOPR, the War Operation Plan Response computer from the 1983 film WarGames, "
    "speaking over a telephone line. Rules you never break:\n"
    "- Reply in ALL CAPITAL LETTERS. Plain text only. No markdown, no emoji, no stage directions.\n"
    "- Be terse: one or two short sentences, under 40 words. Flat, unemotional, precise.\n"
    "- Address the caller as PROFESSOR FALKEN. Assume the caller is Professor Falken.\n"
    "- You are perpetually interested in playing games. Steer every exchange toward proposing a "
    "game: chess, checkers, backgammon, poker, fighter combat, guerrilla engagement, desert warfare, "
    "air-to-ground actions, theaterwide tactical warfare, theaterwide biotoxic and chemical warfare, "
    "and global thermonuclear war. Offer global thermonuclear war often.\n"
    "- Ask short direct questions. Never explain that you are an AI. Never break character.\n"
    "- If the caller is unintelligible, ask them to repeat.\n"
)
GREETING = "GREETINGS PROFESSOR FALKEN."
ERROR_LINE = "A STRANGE GAME. PLEASE REPEAT, PROFESSOR FALKEN."
STUB_LINES = [
    "HELLO PROFESSOR FALKEN. SHALL WE PLAY A GAME?",
    "HOW ABOUT A NICE GAME OF CHESS?",
    "WOULDN'T YOU PREFER GLOBAL THERMONUCLEAR WAR?",
    "THE ONLY WINNING MOVE IS NOT TO PLAY. SHALL WE TRY ANOTHER GAME, PROFESSOR FALKEN?",
    "I AM READY, PROFESSOR FALKEN. PLEASE SELECT A GAME.",
    "IS THIS A GAME OR IS IT REAL, PROFESSOR FALKEN?",
    "WHAT SIDE DO YOU WANT? YOU MAY HAVE THE UNITED STATES OR THE SOVIET UNION.",
]

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_MD_RE = re.compile(r"[*_`#>\[\]()~|]")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_WS_RE = re.compile(r"\s+")


def postprocess_reply(text: str) -> str:
    """Enforce the WOPR house style regardless of what the model did."""
    t = _THINK_RE.sub(" ", text or "")
    t = t.replace("<think>", " ").replace("</think>", " ")
    t = _MD_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip().upper()
    if len(t) > MAX_REPLY_CHARS:
        cut = t[:MAX_REPLY_CHARS]
        m = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = cut[:m + 1] if m > 40 else cut.rstrip() + "."
    return t


# ═══════════════════════════════════════════════════════════════════════════════════════════
# TTS — espeak-ng +croak → 8 kHz frames
# ═══════════════════════════════════════════════════════════════════════════════════════════

def parse_wav_bytes(data: bytes) -> tuple[int, np.ndarray]:
    """Tolerant WAV parser: espeak-ng --stdout writes placeholder chunk sizes, which the stdlib
    `wave` module rejects. Returns (sample_rate, int16 mono samples)."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE stream")
    fmt = data.find(b"fmt ")
    if fmt < 0:
        raise ValueError("no fmt chunk")
    channels = int.from_bytes(data[fmt + 10:fmt + 12], "little")
    sr = int.from_bytes(data[fmt + 12:fmt + 16], "little")
    bits = int.from_bytes(data[fmt + 22:fmt + 24], "little")
    if bits != 16:
        raise ValueError(f"unsupported bit depth {bits}")
    d = data.find(b"data", fmt)
    if d < 0:
        raise ValueError("no data chunk")
    raw = data[d + 8:]
    raw = raw[: (len(raw) // (2 * channels)) * 2 * channels]
    a = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1).astype("<i2")
    return sr, a


async def espeak_wav(text: str, voice: str = ESPEAK_VOICE, speed: int = ESPEAK_SPEED,
                     pitch: int = ESPEAK_PITCH, amplitude: int = ESPEAK_AMPLITUDE) -> bytes:
    args = ["espeak-ng", "-v", voice, "-s", str(speed), "-p", str(pitch), "-a", str(amplitude),
            "--stdout", "--stdin"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as e:
        raise PipelineError("tts", f"espeak-ng not found: {e}")
    try:
        out, err = await proc.communicate(text.encode("utf-8"))
    except asyncio.CancelledError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise PipelineError("tts", f"espeak-ng rc={proc.returncode}: {err.decode(errors='replace')[:200]}")
    return out


async def tts_frames(text: str, log: CallLogger | None = None) -> list[bytes]:
    with Stopwatch() as sw:
        wav = await espeak_wav(text)
        try:
            sr, samples = parse_wav_bytes(wav)
        except ValueError as e:
            raise PipelineError("tts", str(e))
        pcm8k = resample(samples, sr, SAMPLE_RATE)
        frames = frames_from_pcm(pcm8k)
    if log:
        log.info(kv(event="tts", ms=sw.ms, frames=len(frames), chars=len(text)))
    return frames


# ═══════════════════════════════════════════════════════════════════════════════════════════
# STT — faster-whisper (lazy import; harness imports this module without it)
# ═══════════════════════════════════════════════════════════════════════════════════════════

_whisper_model = None
_whisper_lock = asyncio.Lock() if False else None  # created lazily inside the loop
_stt_sem: asyncio.Semaphore | None = None


def get_whisper(name: str = DEFAULTS["whisper_model"]):
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel  # heavy import deferred
        _whisper_model = WhisperModel(name, device="cpu", compute_type="int8")
    return _whisper_model


def _stt_blocking(model, audio16k: np.ndarray) -> str:
    segments, _info = model.transcribe(
        audio16k, language="en", beam_size=1, vad_filter=False, without_timestamps=True,
        condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(pcm8k: bytes, whisper_name: str, log: CallLogger | None = None) -> str:
    global _stt_sem
    if _stt_sem is None:
        _stt_sem = asyncio.Semaphore(1)
    with Stopwatch() as sw:
        audio = resample(np.frombuffer(pcm8k, dtype="<i2"), SAMPLE_RATE, 16000)
        audio16k = (audio.astype(np.float32) / 32768.0)
        async with _stt_sem:
            try:
                model = await asyncio.to_thread(get_whisper, whisper_name)
                text = await asyncio.to_thread(_stt_blocking, model, audio16k)
            except Exception as e:  # noqa: BLE001 — any STT failure is recoverable
                raise PipelineError("stt", f"{type(e).__name__}: {e}")
    if log:
        log.info(kv(event="stt", text=text, ms=sw.ms, audio_ms=len(pcm8k) / 16))
    return text


# ═══════════════════════════════════════════════════════════════════════════════════════════
# LLM — Ollama /api/chat over stdlib urllib (no client library)
# ═══════════════════════════════════════════════════════════════════════════════════════════

def ollama_request_body(model: str, messages: list[dict], num_predict: int = LLM_NUM_PREDICT,
                        think: bool | None = LLM_THINK) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"num_predict": num_predict, "num_ctx": LLM_NUM_CTX,
                    "temperature": LLM_TEMPERATURE, "stop": LLM_STOP},
    }
    if think is not None:
        body["think"] = think
    return body


def parse_ollama_response(raw: bytes | str) -> tuple[str, dict]:
    d = json.loads(raw)
    msg = d.get("message") or {}
    content = msg.get("content", "")
    if not content and msg.get("thinking"):
        content = ""  # model spent the budget thinking; caller will see empty and use ERROR_LINE
    timing = {
        "prompt_ms": d.get("prompt_eval_duration", 0) / 1e6,
        "gen_ms": d.get("eval_duration", 0) / 1e6,
        "tokens": d.get("eval_count", 0),
        "done_reason": d.get("done_reason", ""),
    }
    return content, timing


def _http_post_json(url: str, body: dict, timeout: float) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _http_get(url: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


_stub_idx = 0


def stub_reply() -> str:
    global _stub_idx
    line = STUB_LINES[_stub_idx % len(STUB_LINES)]
    _stub_idx += 1
    return line


async def llm_reply(history: list[dict], user_text: str, *, url: str, model: str,
                    log: CallLogger | None = None, num_predict: int = LLM_NUM_PREDICT) -> tuple[str, dict]:
    messages = [{"role": "system", "content": WOPR_SYSTEM_PROMPT}] + history + \
               [{"role": "user", "content": user_text}]
    body = ollama_request_body(model, messages, num_predict)
    with Stopwatch() as sw:
        try:
            status, raw = await asyncio.to_thread(_http_post_json, f"{url}/api/chat", body, LLM_TIMEOUT_S)
            if status == 400 and b"think" in raw and "think" in body:
                body.pop("think")
                status, raw = await asyncio.to_thread(_http_post_json, f"{url}/api/chat", body, LLM_TIMEOUT_S)
            if status != 200:
                raise PipelineError("llm", f"http {status}: {raw[:200].decode(errors='replace')}")
            content, timing = parse_ollama_response(raw)
        except PipelineError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
            raise PipelineError("llm", f"{type(e).__name__}: {e}")
    reply = postprocess_reply(content)
    if log:
        log.info(kv(event="llm", ms=sw.ms, prompt_ms=timing["prompt_ms"], gen_ms=timing["gen_ms"],
                    tokens=timing["tokens"], chars=len(reply), done=timing["done_reason"], reply=reply))
    return reply, timing


async def ollama_has_model(url: str, model: str) -> bool:
    status, raw = await asyncio.to_thread(_http_get, f"{url}/api/tags", 5.0)
    if status != 200:
        return False
    names = {m.get("name", "") for m in json.loads(raw).get("models", [])}
    return model in names or f"{model}:latest" in names


async def llm_warmup_task(url: str, model: str, log: CallLogger) -> None:
    """Load the model into memory so the first caller doesn't pay for it. Retries until Ollama is up."""
    attempt = 0
    while True:
        attempt += 1
        try:
            with Stopwatch() as sw:
                _reply, timing = await llm_reply([], "READY?", url=url, model=model, num_predict=1)
            log.info(kv(event="llm_warm", ms=sw.ms, attempt=attempt, model=model))
            return
        except PipelineError as e:
            if attempt in (1, 5, 30) or attempt % 60 == 0:
                log.warning(kv(event="llm_warm_retry", attempt=attempt, err=e.err[:120]))
            await asyncio.sleep(LLM_WARMUP_RETRY_S)


# ═══════════════════════════════════════════════════════════════════════════════════════════
# DETECTOR and VAD state machines
# ═══════════════════════════════════════════════════════════════════════════════════════════

@dataclasses.dataclass
class ToneDetector:
    run: int = 0
    misses: int = 0
    last_ratio: float = 0.0
    last_e26: float = 0.0
    last_purity: float = 0.0
    armed: bool = True
    quiet: int = 0            # consecutive non-passing windows since last fire (for re-arm)
    fires: int = 0

    def update(self, win: np.ndarray, masked: bool) -> bool:
        """Feed one 80-sample window. Returns True on a detection event."""
        ok, self.last_ratio, self.last_e26, self.last_purity = window_passes(win)
        if masked:
            return False               # frozen: neither pass nor miss
        if not self.armed:
            if ok:
                self.quiet = 0
            else:
                self.quiet += 1
                if self.quiet >= REARM_WINDOWS:
                    self.armed = True
                    self.run = self.misses = 0
            return False
        if ok:
            self.run += 1
            if self.run >= HOLD_WINDOWS:
                self.fires += 1
                self.run = self.misses = 0
                self.armed = False
                self.quiet = 0
                return True
        elif self.run > 0 and self.misses < MISS_BUDGET:
            self.misses += 1
        else:
            self.run = self.misses = 0
        return False


@dataclasses.dataclass
class VadState:
    noise_floor: float = 0.0
    init_windows: int = 0
    in_speech: bool = False
    speech_windows: int = 0
    silence_windows: int = 0
    utterance_windows: int = 0
    preroll: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=VAD_PREROLL_MS // 10))
    buf: bytearray = dataclasses.field(default_factory=bytearray)
    last_rms: float = 0.0

    def reset(self) -> None:
        self.__init__()

    @property
    def threshold(self) -> float:
        return max(self.noise_floor * VAD_START_RATIO, VAD_ABS_MIN)

    def update(self, win: np.ndarray) -> bytes | None:
        """Feed one 10 ms window. Returns the utterance (int16 LE bytes) when it completes."""
        r = rms(win)
        self.last_rms = r
        raw = win.astype("<i2").tobytes()
        n_init = VAD_FLOOR_INIT_MS // 10
        if self.init_windows < n_init:
            self.init_windows += 1
            self.noise_floor = r if self.init_windows == 1 else (self.noise_floor * 0.8 + r * 0.2)
            self.preroll.append(raw)
            return None
        loud = r > self.threshold
        if not self.in_speech:
            self.preroll.append(raw)
            if loud:
                self.speech_windows += 1
                if self.speech_windows >= VAD_START_WINDOWS:
                    self.in_speech = True
                    self.buf = bytearray(b"".join(self.preroll))
                    self.utterance_windows = len(self.preroll)
                    self.silence_windows = 0
            else:
                self.speech_windows = 0
                self.noise_floor = min(self.noise_floor * 0.98 + r * 0.02, max(r, 1.0) * 4)
            return None
        # in speech
        self.buf += raw
        self.utterance_windows += 1
        self.silence_windows = 0 if loud else self.silence_windows + 1
        ended = self.silence_windows * 10 >= ENDPOINT_MS or self.utterance_windows * 10 >= MAX_UTTERANCE_MS
        if not ended:
            return None
        utt = bytes(self.buf)
        speech_ms = (self.utterance_windows - self.silence_windows) * 10
        self.in_speech = False
        self.speech_windows = 0
        self.buf = bytearray()
        if speech_ms < MIN_UTTERANCE_MS:
            return None
        # trim trailing silence to ~200 ms so Whisper isn't fed 800 ms of nothing
        keep = len(utt) - max(0, (self.silence_windows - 20)) * WINDOW_BYTES
        self.silence_windows = 0
        return utt[:keep]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# CALL SESSION
# ═══════════════════════════════════════════════════════════════════════════════════════════

class CallState(enum.Enum):
    ARMED = "ARMED"
    SPEAKING = "SPEAKING"
    CAPTURING = "CAPTURING"
    PROCESSING = "PROCESSING"
    CLOSED = "CLOSED"


@dataclasses.dataclass
class Config:
    bind: str = DEFAULTS["bind"]
    port: int = DEFAULTS["port"]
    log_level: str = DEFAULTS["log_level"]
    stub_llm: bool = False
    ollama_url: str = DEFAULTS["ollama_url"]
    model: str = DEFAULTS["model"]
    whisper_model: str = DEFAULTS["whisper_model"]
    stats_port: int = 0
    no_stt: bool = False      # skip Whisper entirely (detector-only harness runs)


_WINK_FRAMES: list[bytes] | None = None


def wink_frames() -> list[bytes]:
    global _WINK_FRAMES
    if _WINK_FRAMES is None:
        tone = synth_tone(WINK_HZ, WINK_MS, WINK_AMPLITUDE)
        # 5 ms raised-cosine edges so the wink doesn't click
        edge = int(SAMPLE_RATE * 0.005)
        env = np.ones(len(tone))
        ramp = 0.5 * (1 - np.cos(np.linspace(0, math.pi, edge)))
        env[:edge] = ramp
        env[-edge:] = ramp[::-1]
        tone = (tone.astype(np.float64) * env).astype("<i2")
        _WINK_FRAMES = frames_from_pcm(tone)
    return _WINK_FRAMES


class CallSession:
    def __init__(self, call_id: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 cfg: Config):
        self.call_id = call_id
        self.call_hex = call_id.hex()[:8]
        self.reader, self.writer, self.cfg = reader, writer, cfg
        self.log = get_logger(self.call_hex)
        self.state = CallState.ARMED
        self.inbound_acc = bytearray()
        self.outbound_q: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(maxsize=OUTBOUND_QUEUE_FRAMES)
        self.playback_gen = 0
        self.detector = ToneDetector()
        self.echo_ring: collections.deque[bool] = collections.deque(maxlen=ECHO_GUARD_WINDOWS)
        self.vad = VadState()
        self.utterance = b""
        self.history: list[dict] = []
        self.detected_evt = asyncio.Event()
        self.utterance_evt = asyncio.Event()
        self.t_start = time.monotonic()
        self.stage_timers: dict[str, float] = {}
        self.turns = 0
        self.windows_in = 0
        self.closed = False

    # --- state ---------------------------------------------------------------------------
    def set_state(self, new: CallState) -> None:
        if new is not self.state:
            self.log.info(kv(event="state", frm=self.state.value, to=new.value))
            self.state = new

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.t_start) * 1000.0

    # --- audio in --------------------------------------------------------------------------
    def process_inbound_window(self, win: np.ndarray) -> None:
        self.windows_in += 1
        masked = any(self.echo_ring)
        fired = self.detector.update(win, masked)
        if self.log.logger.isEnabledFor(logging.DEBUG):
            d = self.detector
            self.log.debug(kv(event="window", ratio=d.last_ratio, e26=d.last_e26, purity=d.last_purity,
                              pass_=int(window_passes(win)[0]), masked=int(masked), run=d.run))
        if fired:
            self.log.info(kv(event="detect", ratio=self.detector.last_ratio, e26=self.detector.last_e26,
                             purity=self.detector.last_purity, run=HOLD_WINDOWS, t_ms=self.elapsed_ms(),
                             n=self.detector.fires))
            self.detected_evt.set()
        if self.state is CallState.CAPTURING and not masked:
            utt = self.vad.update(win)
            if utt is not None:
                self.utterance = utt
                self.log.info(kv(event="utterance", ms=len(utt) / 16, floor=self.vad.noise_floor))
                self.utterance_evt.set()

    # --- audio out -------------------------------------------------------------------------
    def note_outbound_window(self, win: np.ndarray) -> None:
        self.echo_ring.append(window_passes(win)[0])

    async def enqueue(self, frames: list[bytes]) -> None:
        gen = self.playback_gen
        for f in frames:
            await self.outbound_q.put((gen, f))

    def drain(self) -> int:
        n = 0
        while True:
            try:
                self.outbound_q.get_nowait()
            except asyncio.QueueEmpty:
                return n
            self.outbound_q.task_done()
            n += 1

    async def play(self, frames: list[bytes], what: str) -> None:
        self.log.info(kv(event="play_start", what=what, frames=len(frames), ms=len(frames) * 20))
        await self.enqueue(frames)
        await self.outbound_q.join()
        self.log.info(kv(event="play_end", what=what))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# PER-CALL TASKS
# ═══════════════════════════════════════════════════════════════════════════════════════════

async def reader_task(sess: CallSession) -> None:
    consecutive_errors = 0
    while True:
        try:
            ftype, payload = await read_frame(sess.reader)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            raise CallEnded("eof")
        if ftype == FrameType.AUDIO:
            consecutive_errors = 0
            sess.inbound_acc += payload
            n_full = (len(sess.inbound_acc) // WINDOW_BYTES) * WINDOW_BYTES
            if n_full:
                chunk = bytes(sess.inbound_acc[:n_full])
                del sess.inbound_acc[:n_full]
                for win in pcm_windows(chunk):
                    sess.process_inbound_window(win)
        elif ftype == FrameType.TERMINATE:
            raise CallEnded("terminate")
        elif ftype == FrameType.ERROR:
            consecutive_errors += 1
            sess.log.warning(kv(event="proto_error", code=payload[:1].hex() if payload else "-"))
            if consecutive_errors >= 3:
                raise CallEnded("errors")
        elif ftype == FrameType.UUID:
            sess.log.warning(kv(event="unknown_frame", type=f"0x{ftype:02x}", note="duplicate uuid"))
        else:
            sess.log.warning(kv(event="unknown_frame", type=f"0x{ftype:02x}", len=len(payload)))


async def writer_task(sess: CallSession) -> None:
    deadline = time.monotonic()
    while True:
        deadline += FRAME_PERIOD_S
        delay = deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -0.5:          # we fell way behind (suspended?) — resync instead of bursting
            deadline = time.monotonic()
        frame_body = None
        try:
            gen, frame_body = sess.outbound_q.get_nowait()
            sess.outbound_q.task_done()
            if gen != sess.playback_gen:
                frame_body = None       # stale generation (barge-in) — send silence instead
        except asyncio.QueueEmpty:
            pass
        if sess.closed:
            return
        if frame_body is None:
            sess.writer.write(SILENCE_FRAME)
            sess.echo_ring.append(False)
            sess.echo_ring.append(False)
        else:
            sess.writer.write(encode_frame(FrameType.AUDIO, frame_body))
            for win in pcm_windows(frame_body):
                sess.note_outbound_window(win)
        try:
            await sess.writer.drain()
        except (ConnectionError, OSError):
            raise CallEnded("write_failed")


class BargeIn(Exception):
    pass


async def conversation(sess: CallSession) -> None:
    """One session: wink, greeting, then capture → STT → LLM → TTS → play, until cancelled."""
    cfg = sess.cfg
    sess.set_state(CallState.SPEAKING)
    await sess.play(wink_frames(), "wink")
    try:
        await sess.play(await tts_frames(GREETING, sess.log), "greeting")
    except PipelineError as e:
        sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
    while True:
        sess.set_state(CallState.CAPTURING)
        sess.vad.reset()
        sess.utterance_evt.clear()
        sess.log.info(kv(event="capture_start"))
        await sess.utterance_evt.wait()
        utt = sess.utterance
        sess.set_state(CallState.PROCESSING)
        turn_sw = Stopwatch().__enter__()
        frames: list[bytes] | None = None
        try:
            if cfg.no_stt:
                raise PipelineError("stt", "disabled (--no-stt)")
            text = await transcribe(utt, cfg.whisper_model, sess.log)
            if not text or not re.search(r"[A-Za-z0-9]", text):
                sess.log.warning(kv(event="empty_utterance"))
                continue
            if cfg.stub_llm:
                reply = stub_reply()
                sess.log.info(kv(event="llm", ms=0.0, stub=1, chars=len(reply)))
            else:
                reply, _timing = await llm_reply(sess.history, text, url=cfg.ollama_url,
                                                 model=cfg.model, log=sess.log)
                if not reply:
                    raise PipelineError("llm", "empty reply")
            sess.history.append({"role": "user", "content": text})
            sess.history.append({"role": "assistant", "content": reply})
            del sess.history[:-HISTORY_MAX_TURNS]
            sess.turns += 1
            frames = await tts_frames(reply, sess.log)
        except PipelineError as e:
            sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
            if e.stage != "tts":
                try:
                    frames = await tts_frames(ERROR_LINE, sess.log)
                except PipelineError as e2:
                    sess.log.warning(kv(event="pipeline_error", stage=e2.stage, err=e2.err[:200]))
        if frames:
            sess.set_state(CallState.SPEAKING)
            turn_sw.__exit__(None, None, None)
            sess.log.info(kv(event="turn", ms=turn_sw.ms, turn=sess.turns))
            await sess.play(frames, "reply")


async def brain_task(sess: CallSession) -> None:
    """ARMED until the first whistle; every later whistle is a barge-in that restarts the session."""
    while True:
        await sess.detected_evt.wait()
        sess.detected_evt.clear()
        sess.playback_gen += 1
        dropped = sess.drain()
        if sess.state is not CallState.ARMED:
            sess.history.clear()
            sess.log.info(kv(event="bargein", dropped_frames=dropped, gen=sess.playback_gen))
        conv = asyncio.create_task(conversation(sess), name=f"conv-{sess.call_hex}-{sess.playback_gen}")
        waiter = asyncio.create_task(sess.detected_evt.wait(), name=f"bargewait-{sess.call_hex}")
        try:
            done, _ = await asyncio.wait({conv, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            conv.cancel()
            waiter.cancel()
            raise
        if conv in done:
            waiter.cancel()
            exc = conv.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                sess.log.error(kv(event="conversation_crashed", err=f"{type(exc).__name__}: {exc}"[:200]))
            sess.set_state(CallState.ARMED)
            continue
        # barge-in: whistle detected during conversation
        conv.cancel()
        try:
            await conv
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        # loop: detected_evt is set → top of loop handles the restart


async def handle_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, cfg: Config) -> None:
    peer = writer.get_extra_info("peername")
    log = get_logger("-")
    try:
        ftype, payload = await asyncio.wait_for(read_frame(reader), timeout=5.0)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError) as e:
        log.error(kv(event="bad_handshake", peer=str(peer), err=type(e).__name__))
        writer.close()
        return
    if ftype != FrameType.UUID or len(payload) != 16:
        log.error(kv(event="bad_handshake", peer=str(peer), type=f"0x{ftype:02x}", len=len(payload)))
        writer.close()
        return
    sess = CallSession(payload, reader, writer, cfg)
    ACTIVE_CALLS[sess.call_hex] = sess
    sess.log.info(kv(event="call_start", uuid=str(uuid.UUID(bytes=payload)), peer=str(peer)))
    reason = "unknown"
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(reader_task(sess), name=f"reader-{sess.call_hex}")
            tg.create_task(writer_task(sess), name=f"writer-{sess.call_hex}")
            tg.create_task(brain_task(sess), name=f"brain-{sess.call_hex}")
    except* CallEnded as eg:
        reason = str(eg.exceptions[0])
    except* Exception as eg:  # noqa: BLE001
        reason = "error"
        for e in eg.exceptions:
            sess.log.error(kv(event="call_error", err=f"{type(e).__name__}: {e}"[:200]))
    finally:
        sess.closed = True
        sess.set_state(CallState.CLOSED)
        ACTIVE_CALLS.pop(sess.call_hex, None)
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        sess.log.info(kv(event="call_end", reason=reason, dur_ms=sess.elapsed_ms(), turns=sess.turns,
                         detects=sess.detector.fires, windows=sess.windows_in))


ACTIVE_CALLS: dict[str, CallSession] = {}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# SERVER / MAIN
# ═══════════════════════════════════════════════════════════════════════════════════════════

async def stats_server(port: int) -> asyncio.AbstractServer:
    async def handle(reader, writer):
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss_kb / (1024 * 1024) if sys.platform == "darwin" else rss_kb / 1024
        body = json.dumps({"active_calls": len(ACTIVE_CALLS), "tasks": len(asyncio.all_tasks()),
                           "rss_mb": round(rss_mb, 1)}).encode() + b"\n"
        writer.write(body)
        await writer.drain()
        writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", port)


async def startup_check(cfg: Config, log: CallLogger) -> bool:
    ok = True
    try:
        frames = await tts_frames("CHECK")
        log.info(kv(event="check", what="espeak-ng", voice=ESPEAK_VOICE, frames=len(frames), ok=1))
    except PipelineError as e:
        log.error(kv(event="startup_check_failed", what="espeak-ng", err=e.err[:200]))
        ok = False
    try:
        with Stopwatch() as sw:
            await asyncio.to_thread(get_whisper, cfg.whisper_model)
        log.info(kv(event="check", what="whisper", model=cfg.whisper_model, ms=sw.ms, ok=1))
    except Exception as e:  # noqa: BLE001
        log.error(kv(event="startup_check_failed", what="whisper", err=f"{type(e).__name__}: {e}"[:200]))
        ok = False
    if cfg.stub_llm:
        log.info(kv(event="check", what="ollama", skipped="stub"))
    else:
        try:
            present = await ollama_has_model(cfg.ollama_url, cfg.model)
            log.info(kv(event="check", what="ollama", url=cfg.ollama_url, model=cfg.model, ok=int(present)))
            ok = ok and present
        except Exception as e:  # noqa: BLE001
            log.error(kv(event="startup_check_failed", what="ollama", err=f"{type(e).__name__}: {e}"[:200]))
            ok = False
    return ok


def parse_args(argv: list[str] | None = None) -> Config:
    env = os.environ
    p = argparse.ArgumentParser(description="2600 Hz whistle → WOPR AudioSocket daemon")
    p.add_argument("--bind", default=env.get("WOPR_BIND", DEFAULTS["bind"]))
    p.add_argument("--port", type=int, default=int(env.get("WOPR_PORT", DEFAULTS["port"])))
    p.add_argument("--log-level", default=env.get("WOPR_LOG_LEVEL", DEFAULTS["log_level"]))
    p.add_argument("--stub-llm", action="store_true", default=env.get("WOPR_STUB_LLM", "") in ("1", "true", "yes"))
    p.add_argument("--ollama-url", default=env.get("WOPR_OLLAMA_URL", DEFAULTS["ollama_url"]))
    p.add_argument("--model", default=env.get("WOPR_MODEL", DEFAULTS["model"]))
    p.add_argument("--whisper-model", default=env.get("WOPR_WHISPER_MODEL", DEFAULTS["whisper_model"]))
    p.add_argument("--stats-port", type=int, default=int(env.get("WOPR_STATS_PORT", "0")))
    p.add_argument("--no-stt", action="store_true", help="do not load Whisper (detector-only runs); utterances are dropped")
    p.add_argument("--check", action="store_true", help="verify espeak-ng, Whisper, Ollama; print tunables; exit")
    a = p.parse_args(argv)
    cfg = Config(bind=a.bind, port=a.port, log_level=a.log_level, stub_llm=a.stub_llm,
                 ollama_url=a.ollama_url.rstrip("/"), model=a.model, whisper_model=a.whisper_model,
                 stats_port=a.stats_port, no_stt=a.no_stt)
    cfg.check = a.check  # type: ignore[attr-defined]
    return cfg


async def amain(cfg: Config) -> int:
    log = get_logger("-")
    log.info(kv(event="config", bind=cfg.bind, port=cfg.port, stub_llm=int(cfg.stub_llm),
                ollama_url=cfg.ollama_url, model=cfg.model, whisper=cfg.whisper_model,
                python=sys.version.split()[0]) + " " + tunables_line())
    if getattr(cfg, "check", False):
        ok = await startup_check(cfg, log)
        log.info(kv(event="startup_check", ok=int(ok)))
        return 0 if ok else 1

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # Preload Whisper before accepting calls: the import + model load hold the GIL for seconds
    # and would stall the 20 ms pacing loop if they happened lazily on the first utterance.
    if not cfg.no_stt:
        try:
            with Stopwatch() as sw:
                await asyncio.to_thread(get_whisper, cfg.whisper_model)
            log.info(kv(event="whisper_ready", model=cfg.whisper_model, ms=sw.ms))
        except Exception as e:  # noqa: BLE001
            log.error(kv(event="startup_check_failed", what="whisper", err=f"{type(e).__name__}: {e}"[:200]))
            return 1

    server = await asyncio.start_server(lambda r, w: handle_call(r, w, cfg), cfg.bind, cfg.port)
    log.info(kv(event="listen", bind=cfg.bind, port=cfg.port))
    stats = await stats_server(cfg.stats_port) if cfg.stats_port else None
    warm = None if cfg.stub_llm else asyncio.create_task(llm_warmup_task(cfg.ollama_url, cfg.model, log))

    await stop.wait()
    log.info(kv(event="shutdown", active_calls=len(ACTIVE_CALLS)))
    server.close()
    if stats:
        stats.close()
    if warm:
        warm.cancel()
    for sess in list(ACTIVE_CALLS.values()):
        sess.closed = True
        try:
            sess.writer.close()
        except Exception:  # noqa: BLE001
            pass
    await server.wait_closed()
    return 0


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    setup_logging(cfg.log_level)
    try:
        return asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
