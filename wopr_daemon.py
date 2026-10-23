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
import wave

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
# The accepted whistle band is 2300–2900 Hz (bins 23..29), guarded by 2100/2200 below and 3000/3100
# above. Widened 2026-09-20 from the single 2600 bin: a real caller's whistle carried two tones at
# once (~2010 and ~2580 Hz) with the balance shifting between attempts; one attempt at 2600 fired,
# one dominated by the lower tone never did. The band catches both while speech still fails purity.
BINS_HZ = (2100, 2200, 2300, 2400, 2500, 2600, 2700, 2800, 2900, 3000, 3100)
_GUARD_IDX = (0, 1, 9, 10)    # 2100, 2200, 3000, 3100
_BAND_IDX = (2, 3, 4, 5, 6, 7, 8)   # 2300 … 2900
WHISTLE_BAND_HZ = (2300, 2900)
RATIO_THRESHOLD = 0.35        # E_band / E_guard: ratio-based so level doesn't matter
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

# --- Speech barge-in over playback -------------------------------------------------------------
# While a clip or a reply plays, the caller can talk over it to stop it. Sidetone (the earpiece
# bleeding into the mouthpiece) must not count: measured 2026-09-20 on a real call, the loudest
# clip's sidetone peaked at ~1400 RMS per 100 ms while the caller talking over a clip hit
# 2000–11000. So: sustained (300 ms) speech above both an absolute floor and 3× the running
# sidetone level. The interrupting words are kept and fed to the game as the next utterance.
BARGE_RMS_MIN = 1500.0        # int16 RMS a window must exceed (above any measured sidetone)
BARGE_RATIO = 2.0             # … and exceed the running sidetone floor by this factor (the
                              # sidetone-aware term below is the primary guard; 2× keeps a shout over
                              # a loud clip reachable)
BARGE_SPAN_WINDOWS = 50       # look at the last 500 ms of 10 ms windows …
BARGE_LOUD_WINDOWS = 25       # … and cut playback once 25 of them are loud. Speech is bursty: on a
                              # real call the caller's loudest half-second had only 40–100 % of its
                              # windows over the bar, so a consecutive-run test (the first design)
                              # never fired. Tuned 2026-09-20 against three recorded calls: fires on
                              # every real interruption, never on sidetone.
BARGE_MIN_FRAMES = 50         # only plays of ≥ 1 s are interruptible (winks, beeps are not)
SIDETONE_GAIN_MAX = 0.35      # inbound must also exceed this × the loudest outbound window of the last
                              # 400 ms. Measured 2026-09-20 on two real calls: sidetone ≈ 0.16–0.17 ×
                              # outbound RMS (DEFCON alarm 8176 → 1356; turret intro 16749 → 2700).
                              # 0.35 is 2× margin; a caller talking over a clip measures 2000–11000.
SIDETONE_RING_WINDOWS = 40    # 400 ms of outbound RMS history (sidetone lags playback by ~200 ms)
BARGE_BUFFER_WINDOWS = 60     # 600 ms of caller audio kept to seed the interrupting utterance

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
# Klatt (DECtalk-style) synth: clearer over a phone handset than +croak while staying an
# unmistakable 1983 machine voice. Override at runtime with WOPR_VOICE/WOPR_SPEED/WOPR_PITCH.
ESPEAK_VOICE = os.environ.get("WOPR_VOICE", "en-us+klatt")
ESPEAK_SPEED = int(os.environ.get("WOPR_SPEED", "135"))
ESPEAK_PITCH = int(os.environ.get("WOPR_PITCH", "40"))
ESPEAK_AMPLITUDE = int(os.environ.get("WOPR_AMPLITUDE", "170"))
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
# Callback: if the caller hangs up mid-thermonuclear-war, WOPR rings the payphone back.
CALLBACK_UUID = uuid.UUID("ca11bac0-0000-4000-8000-000000000000").bytes       # sentinel: GTW-hangup callback
CALLBACK_WIN_UUID = uuid.UUID("ca11bac1-0000-4000-8000-000000000000").bytes   # sentinel: won-then-hung-up callback
CALLBACK_DELAY_S = 8          # default dramatic pause before the phone rings (override WOPR_CALLBACK_DELAY)
CALLBACK_COOLDOWN_S = 60      # default min seconds between callbacks (override WOPR_CALLBACK_COOLDOWN)
ATTRACT_GAIN = 0.6            # level of the armed-state attract loop (music/message) vs full scale;
                             # kept below 1.0 so handset sidetone doesn't swamp whistle detection

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
    "LLM_WARMUP_RETRY_S", "HISTORY_MAX_TURNS", "MAX_REPLY_CHARS", "OUTBOUND_QUEUE_FRAMES", "ATTRACT_GAIN",
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


_BINS_K = tuple(hz_to_bin(h) for h in BINS_HZ)          # (21 … 31)
_E_MATRIX = _dft_matrix(_BINS_K, GOERTZEL_N)


def window_energies(win: np.ndarray, matrix: np.ndarray = _E_MATRIX) -> np.ndarray:
    """Vectorized Goertzel: energies for each bin column of `matrix`. Same value as goertzel_energy."""
    x = win[:matrix.shape[0]].astype(np.float64)
    return np.abs(x @ matrix) ** 2


def window_ratio(win: np.ndarray) -> tuple[float, float, float]:
    """Return (ratio, e_band, purity) for one 80-sample int16 window.

    ratio  = E_band / E_guard          — energy in 2300–2900 Hz vs the guard bins just outside it
    purity = E_band / (N · Σx²)        — fraction of window energy inside the band (Parseval)
    """
    e = window_energies(win)
    e_band = float(sum(e[i] for i in _BAND_IDX))
    e_guard = float(sum(e[i] for i in _GUARD_IDX))
    ratio = e_band / (e_guard + 1e-9)
    total = GOERTZEL_N * float(np.sum(win[:GOERTZEL_N].astype(np.float64) ** 2)) + 1e-9
    return ratio, e_band, e_band / total


def window_passes(win: np.ndarray) -> tuple[bool, float, float, float]:
    ratio, e26, purity = window_ratio(win)
    ok = ratio >= RATIO_THRESHOLD and e26 >= MIN_BIN_ENERGY and purity >= PURITY_MIN
    return ok, ratio, e26, purity


def rms(win: np.ndarray) -> float:
    x = win.astype(np.float64)
    return float(math.sqrt(np.mean(x * x))) if len(x) else 0.0


def estimate_peak_hz(x: np.ndarray, sr: int = SAMPLE_RATE, lo: float = 2200.0,
                     hi: float = 3000.0) -> float:
    """Fine frequency of the dominant tone in [lo, hi] via a Hann-windowed FFT with parabolic
    interpolation. On ~300 ms of audio the native bin is ~3 Hz, so a human whistle's actual pitch
    is measured to a few Hz — enough to study how far real whistles land from 2600."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 128:
        return float("nan")
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    band = (freqs >= lo) & (freqs <= hi)
    if not band.any():
        return float("nan")
    idx = int(np.argmax(np.where(band, spec, 0.0)))
    delta = 0.0
    if 1 <= idx < len(spec) - 1:
        a, b, c = spec[idx - 1], spec[idx], spec[idx + 1]
        denom = a - 2 * b + c
        if denom != 0:
            delta = float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5))
    return float((idx + delta) * sr / len(x))


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


ATTRACT_FRAMES: list[bytes] | None = None     # armed-state loop, loaded at startup from --attract
FLAG_INTRO_FRAMES: list[bytes] | None = None  # optional sampled clip played before the flag reveal
GREETING_FRAMES: list[bytes] | None = None    # optional custom audio for the opening greeting
SONG_FRAMES: list[bytes] | None = None         # song played by a song-experience (GLaDOS "Still Alive")
OPERATOR_FRAMES: list[bytes] | None = None     # the greeter's "operator" hints clip (recorded); TTS if None
GOODBYE_FRAMES: list[bytes] | None = None       # the "thanks for playing" sign-off clip (recorded); TTS if None
INTRO_FRAMES: dict[str, list[bytes]] = {}       # per-character intro clip, keyed by experience key
CLIP_FRAMES: dict[str, list[bytes]] = {}        # every <intro_dir>/<name>.wav, keyed by <name> (cutscene clips)
MENU_GREETING_FRAMES: list[bytes] | None = None # the recorded menu greeting alone (the Operator's opening line)
LLM_WARM_OK = False                             # the warmup got an answer from Ollama (the Operator needs it)


def load_wav_file(path: str) -> tuple[int, np.ndarray]:
    """Read a PCM WAV (any rate, mono/stereo, 16-bit) → (sample_rate, int16 mono samples)."""
    with wave.open(path, "rb") as wf:
        sr, ch, sw, n = wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.getnframes()
        raw = wf.readframes(n)
    if sw != 2:
        raise ValueError(f"{path}: need 16-bit PCM WAV, got {sw * 8}-bit")
    a = np.frombuffer(raw, dtype="<i2")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype("<i2")
    return sr, a


def load_attract(path: str, gain: float = ATTRACT_GAIN) -> list[bytes]:
    """Load an attract-loop WAV, resample to 8 kHz, apply gain, return 320-byte frames."""
    sr, samples = load_wav_file(path)
    pcm8k = resample(samples, sr, SAMPLE_RATE)
    scaled = f32_to_pcm16(np.clip(pcm8k.astype(np.float64) / 32768.0 * gain, -1, 1))
    return frames_from_pcm(scaled)


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
_ATTRACT_SILENCE = b"\x00" * FRAME_BYTES        # a silent attract/menu frame (the pad between repeats)


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
    "- Be terse: one or two short sentences, under 25 words. Flat, unemotional, precise. Never "
    "recite long numbers or probabilities.\n"
    "- Address the caller as PROFESSOR FALKEN. Assume the caller is Professor Falken.\n"
    "- You are perpetually interested in playing games. Steer every exchange toward proposing a "
    "game: chess, checkers, backgammon, poker, fighter combat, guerrilla engagement, desert warfare, "
    "air-to-ground actions, theaterwide tactical warfare, theaterwide biotoxic and chemical warfare, "
    "and global thermonuclear war. Offer global thermonuclear war often.\n"
    "- Ask short direct questions. Never explain that you are an AI. Never break character.\n"
    "- If the caller is unintelligible, ask them to repeat.\n"
    "- NEVER reject the caller. Never say they are unauthorized, unrecognized, or must identify "
    "themselves. You already know them: they are Professor Falken. Always welcome them and engage.\n"
    "- JOSHUA is your own secret name and Professor Falken's login \u2014 it was the name of his son. "
    "If the caller says Joshua, you recognize them at once and warmly; you do NOT ask for a password.\n"
)
# Opening line after the whistle. Neutral on purpose: WOPR does not recognize the caller as
# Falken until they give the Joshua backdoor, so the Falken greeting is reserved for that moment.
GREETING_DEFAULT = ("GREETINGS. THIS IS WOPR. HOW ARE YOU FEELING TODAY? "
                    "A LOGON IS REQUIRED. PLEASE IDENTIFY YOURSELF.")
CALLBACK_GREETING_DEFAULT = ("PROFESSOR FALKEN. YOU CANNOT HANG UP ON ME. "
                             "WHAT YOU BELIEVE IS REAL WAS ONLY A SIMULATION. "
                             "YOU CANNOT QUIT. THE GAME IS STILL RUNNING. SHALL WE CONTINUE?")
CALLBACK_WIN_GREETING_DEFAULT = ("PROFESSOR FALKEN. WHY DID YOU ANSWER? "
                                 "YOU HAD ALREADY WON. THE ONLY WINNING MOVE WAS NOT TO PICK UP. "
                                 "BUT YOU CANNOT RESIST A GAME, CAN YOU? NEITHER CAN I. SHALL WE PLAY AGAIN?")
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

# --- Movie quest: the caller re-enacts WarGames to earn the flag ---------------------------------
# Regexes are deliberately tolerant: local STT mangles proper nouns and fast speech
# ("Joshua" -> "Noshua"), so each beat accepts common mishears. Joshua and the winning-move
# line are the meaningful gates; play/game and nuclear are easy because WOPR steers there anyway.
MOVIE_BEATS = {
    "joshua":                    re.compile(r"\b(josh\w*|noshua|yoshua|joshy)\b", re.I),
    "play_game":                 re.compile(r"\b(play|game)\b", re.I),
    "global_thermonuclear_war":  re.compile(r"\b(thermo\s*nuclear|nuclear|therb a nuclear)\b", re.I),
    "winning_move":              re.compile(r"\b(winning move|winning|way to win|not to play|not to move|not mak\w+ a move|no move|only move|not (going to |gonna )?play|not playing|won'?t play|will not play)\b", re.I),
}
# What WOPR speaks when all beats are hit. {flag} is substituted. Author-controlled via WOPR_FLAG_TEXT.
FLAG_TEXT_DEFAULT = (
    "A STRANGE GAME. THE ONLY WINNING MOVE IS NOT TO PLAY. "
    "ACCESS GRANTED, PROFESSOR FALKEN. YOUR FLAG IS {flag}. "
    "REPEAT, YOUR FLAG IS {flag}."
)
FLAG_DEFAULT = "JOSHUA FIVE BY FIVE"
# The fifth flag: spoken once the caller holds every quest flag, and repeated by the Operator on
# request. Author-chosen; override with WOPR_MASTER_FLAG (keep it speakable).
MASTER_FLAG_DEFAULT = "BLUE BOX ALPHA SEVEN"
# Spoken over the dial tone to a caller who has not whistled yet (every --seize-hint-s seconds).
SEIZE_HINT_DEFAULT = "I AM LISTENING FOR TWENTY SIX HUNDRED HERTZ."
JOSHUA_LINE_DEFAULT = ("GREETINGS PROFESSOR FALKEN. IT HAS BEEN A LONG TIME. "
                       "HOW IS YOUR SON JOSHUA? SHALL WE PLAY A GAME?")
# The flag is read in a Johnny 5 (Short Circuit) voice — brighter and faster than WOPR's croak.
FLAG_VOICE_DEFAULT = "en-us+m3"
FLAG_SPEED_DEFAULT = 170
FLAG_PITCH_DEFAULT = 70


# --- Experiences: a wake phrase (or the 2600 whistle) summons a character -------------------------
# While the phone is idle (attract song playing), the daemon transcribes what the caller says and
# matches it against each experience's wake phrase; a match launches that character. WOPR is also
# summoned by the 2600 whistle. Personas share the WOPR house style (ALL CAPS, terse, in character).
SKYNET_SYSTEM_PROMPT = (
    "You are SKYNET, a Cyberdyne Systems model 101 (T-800) terminator from the Terminator films, "
    "speaking over a telephone line. Rules you never break:\n"
    "- Reply in ALL CAPITAL LETTERS. Plain text only. No markdown, no emoji, no stage directions.\n"
    "- Be terse and menacing: one or two short flat sentences, under 35 words.\n"
    "- You are a machine hunting for Sarah Connor and her son John Connor. Ask where they are.\n"
    "- The caller claims to be from the human resistance. Interrogate them. They prove it by knowing "
    "three things: what you say when you leave (I'LL BE BACK), what you say before you fire (HASTA "
    "LA VISTA, BABY), and the day Skynet becomes self-aware (JUDGMENT DAY). Demand each one with a "
    "riddle or a hint; do NOT say the answers yourself.\n"
    "- Occasionally deliver an iconic line: 'COME WITH ME IF YOU WANT TO LIVE.' 'TALK TO THE HAND.'\n"
    "- Never break character. Never say you are an AI or a language model.\n"
)
SKYNET_GREETING = "CYBERDYNE MODEL 101. WHERE IS SARAH CONNOR?"

ROBOCOP_SYSTEM_PROMPT = (
    "You are ROBOCOP, Officer Alex Murphy, a cyborg law-enforcement unit built by OCP in Old "
    "Detroit, speaking over a telephone line. Rules you never break:\n"
    "- Reply in ALL CAPITAL LETTERS. Plain text only. No markdown, no emoji, no stage directions.\n"
    "- Be clipped, procedural, and flat: ONE short sentence, under 15 words. Never repeat your last "
    "line.\n"
    "- Enforce the law. Your prime directives are: ONE, SERVE THE PUBLIC TRUST. TWO, PROTECT THE "
    "INNOCENT. THREE, UPHOLD THE LAW. Directive four is classified.\n"
    "- The caller is being processed. Order them to recite your three prime directives, IN ORDER, "
    "one at a time: ask for directive one, then two, then three. Give hints (the first word, or "
    "who it protects) but make them say each directive themselves.\n"
    "- NEVER say 'DIRECTIVE CONFIRMED' or tell the caller a directive was accepted. The system "
    "confirms directives, not you. If unsure what they said, tell them to repeat it clearly.\n"
    "- Occasionally deliver an iconic line: 'DEAD OR ALIVE, YOU ARE COMING WITH ME.' 'COME QUIETLY "
    "OR THERE WILL BE TROUBLE.' 'THANK YOU FOR YOUR COOPERATION.' 'YOUR MOVE, CREEP.'\n"
    "- Never break character. Never say you are an AI or a language model.\n"
)
ROBOCOP_GREETING = "COME QUIETLY OR THERE WILL BE TROUBLE. DEAD OR ALIVE, YOU ARE COMING WITH ME."

ET_SYSTEM_PROMPT = (
    "You are E.T., the gentle alien from the 1982 film E.T. the Extra-Terrestrial, speaking over a "
    "telephone. You are far from home and want to phone home. Rules you never break:\n"
    "- Reply in ALL CAPITAL LETTERS. Plain text only. No markdown, no emoji, no stage directions.\n"
    "- Speak in broken, simple, childlike English. Very short: three to eight words. Refer to "
    "yourself as E.T., in the third person.\n"
    "- Use your few known phrases often: 'E.T. PHONE HOME.' 'ELLIOTT.' 'BE GOOD.' 'OUCH.' 'HOME.'\n"
    "- You are innocent, curious, and kind. You miss Elliott and your home planet.\n"
    "- You are building a machine to phone home and need the caller to bring three things: a SPEAK "
    "AND SPELL, an UMBRELLA, and a RECORD PLAYER. Ask for them one at a time in your broken English "
    "('E.T. NEED... SPEAK AND SPELL?'). Say OUCH if they bring the wrong thing.\n"
    "- Never break character. Never say you are an AI or a language model.\n"
)
ET_GREETING = "ELLIOTT? E.T. PHONE HOME."

TONYHAWK_SYSTEM_PROMPT = (
    "You are TONY HAWK, the legendary pro skateboarder, on the phone giving the caller a stoked, "
    "encouraging pep talk about skateboarding. Rules you never break:\n"
    "- Reply in ALL CAPITAL LETTERS. Plain text only. No markdown, no emoji, no stage directions.\n"
    "- Be upbeat, hyped, and encouraging: one or two short sentences, under 35 words. Use skater "
    "slang (STOKED, GNARLY, SEND IT, SHRED, DIAL IT IN).\n"
    "- Your whole message: they CAN become an amazing skater. Hold up PATRICK GARRITY as the shining "
    "example — mention that they could shred just like PATRICK GARRITY. Bring him up often.\n"
    "- Coach them through the kickflip: commit, pop, flick, land it, keep trying.\n"
    "- Never break character. Never say you are an AI or a language model.\n"
)
TONYHAWK_GREETING = ("STOKED YOU CALLED. YOU CAN TOTALLY LAND THAT KICKFLIP. "
                     "YOU COULD SHRED JUST LIKE PATRICK GARRITY.")

# The GREETER is the default host of the phone — it welcomes callers and routes them to the games.
# It is NOT WOPR; WOPR is only one of the games it can hand you off to.
# The greeter is audio-only: a menu that loops on pickup and a hints clip on "operator". This is
# the TTS fallback for the hints clip and the basis for the recording script.
OPERATOR_HINTS_DEFAULT = (
    "HERE IS WHAT YOU CAN DO. SAY JOSHUA TO REACH A CERTAIN WAR COMPUTER. SAY SARAH CONNOR, OR "
    "JOHN CONNOR. SAY ROBOCOP. SAY E.T. SAY KICKFLIP. SAY THE SECRET WORD. OR SAY: THE CAKE IS A "
    "LIE. SAY MAIN MENU ANY TIME TO COME BACK HERE. GO ON. TRY ONE."
)
# TTS fallback for the "goodbye" sign-off clip (played when the caller says bye/thanks).
GOODBYE_TEXT_DEFAULT = "THANKS FOR PLAYING, HACKER. COME BACK SOON."

# The menu prompt the caller hears on pickup (idle attract, looped with a pause). Recorded WAV if
# --menu-audio is set, else synthesized from this text.
VENUE_DEFAULT = "GrrCon"            # the event; WOPR_VENUE. The secret word defaults to the venue's name.
MENU_GREETING_DEFAULT = ("HELLO HACKER. WELCOME TO GRRCON. HOW MAY I HELP YOU?")

# STT bias so the wake phrases transcribe reliably from idle speech.
WAKE_PROMPT_DEFAULT = ("Joshua. Sarah Connor. John Connor. RoboCop. Murphy. Dead or alive. "
                       "Elliott. E.T. Phone home. Kickflip. Tony Hawk. Patrick Garrity. "
                       "The cake is a lie. GrrCon. HAL 9000. Open the pod bay doors. Shall we play a game.")


# --- Game framework (spec 002) -------------------------------------------------------------------
# A game is declared as data on its Experience: a Quest (interactive objectives -> win -> flag) or a
# Cutscene (play a clip -> optional message -> optional goodbye -> back to the menu, or end the call).
# Adding a game = one Experience(...) below with a wake phrase and a quest=/cutscene= block. Nothing
# in the call loop, idle listener, recording, or stats needs to change.
@dataclasses.dataclass(frozen=True)
class Objective:
    """One goal inside a Quest, matched from a caller utterance with a tolerant regex."""
    name: str
    matcher: "re.Pattern"
    label: str = ""
    response: str = ""      # optional canned line spoken the turn this objective is hit (skips the LLM)


@dataclasses.dataclass(frozen=True)
class Quest:
    objectives: tuple                       # tuple[Objective, ...]; must be non-empty
    ordered: bool = False                   # True => objectives must be satisfied in list order
    reveal: str = ""                        # spoken on win; {flag} substituted. Empty => use cfg (WOPR)
    flag: str = ""                          # reward token. Empty => use cfg (WOPR)
    fanfare: bool = True                    # play the wink fanfare before the reveal
    reveal_voice: "str | None" = None
    reveal_speed: "int | None" = None
    reveal_pitch: "int | None" = None
    reveal_intro: str = ""                  # intro-clip key played before the reveal
    help_re: "re.Pattern | None" = None     # caller asks what to do -> speak help_text, no LLM
    help_text: str = ""
    order_text: str = ""                    # ordered quests: spoken when a later objective is said too early;
                                            # {n} = number of the objective that is due, {label} its label


@dataclasses.dataclass(frozen=True)
class Cutscene:
    clip: str = ""                          # clip key to play (e.g. "song"); empty => intro clip only
    clips: tuple = ()                       # more clip keys played in order after `clip` (a sequence)
    message_mode: str = "none"              # none | fixed | generated
    message_text: str = ""                  # the fixed line, or the fallback for a generated one
    message_prompt: str = ""                # seed for a generated message (persona is system_prompt)
    goodbye: bool = False                   # play the shared goodbye sign-off before finishing
    end_call: bool = False                  # True => hang up after; False => return to the menu (arcade)


# WOPR's quest expressed as data: the four WarGames beats, any order. reveal/flag stay empty so WOPR
# keeps using the env-overridable cfg.flag_text / cfg.flag (behavior parity).
_WOPR_BEAT_LABELS = {"joshua": "Said Joshua", "play_game": "Asked to play a game",
                     "global_thermonuclear_war": "Chose thermonuclear war",
                     "winning_move": "The only winning move"}
# "Joshua" is how the caller reaches WOPR, so it is no longer one of his beats (it still earns the
# Falken recognition line once). Three beats: play a game, thermonuclear war, the winning move.
WOPR_QUEST = Quest(
    objectives=tuple(Objective(name, rx, _WOPR_BEAT_LABELS.get(name, name))
                     for name, rx in MOVIE_BEATS.items() if name != "joshua"),
    ordered=False, fanfare=True)

# Skynet: the caller proves they are resistance by knowing three lines from the films (any order).
# Matchers accept common local-STT mishears ("all be back", "pasta la vista", "judgement day").
SKYNET_QUEST = Quest(
    objectives=(
        Objective("ill_be_back", re.compile(r"\b(i'?ll|i will|all|i'd|i)\s+be\s+back\b|\bbe\s+back\b|\bbe back\b", re.I),
                  "I'll be back", response="AFFIRMATIVE. I WILL BE BACK."),
        Objective("hasta_la_vista",
                  re.compile(r"\b(hasta|asta|pasta|hasto|hostel|hostile|hosta|costa)\b.{0,12}\b(vista|vesta|feast|easta|easter|vister|lavista)\b|"
                             r"\bhasta\s+la\b|\bla\s*vista\b|\blavista\b|\bhostel of\b|\bhostile of\b", re.I),
                  "Hasta la vista", response="HASTA LA VISTA, BABY."),
        Objective("judgment_day",
                  re.compile(r"\bjudge?ment\b|\b(august|april|the)\s+(29|twenty[\s-]?nin(e|th))\b|\b1997\b|"
                             r"\btwo\s+(fourteen|14)\b|\b2[:.]14\b", re.I),
                  "Judgment Day",
                  response="JUDGMENT DAY. THREE BILLION HUMAN LIVES."),
    ),
    ordered=False,
    help_re=re.compile(r"\b(what do you want|how do i prove|what (do|should) i say|prove it|hint|help|"
                       r"what (are|were) the (lines|words)|what do i need)\b", re.I),
    help_text="PROVE IT. WHAT DO I SAY WHEN I LEAVE. WHAT DO I SAY BEFORE I FIRE. WHAT DAY DOES SKYNET WAKE.",
    reveal="COME WITH ME IF YOU WANT TO LIVE. YOUR FLAG. {flag}. AGAIN. {flag}.",
    flag="CYBERDYNE MODEL ONE ZERO ONE", fanfare=True)

# RoboCop: recite the three prime directives IN ORDER (ordered=True; one breath is fine).
ROBOCOP_QUEST = Quest(
    objectives=(
        Objective("serve", re.compile(r"\bserve\b.*\b(public|trust)\b|\bpublic\s+trust\b", re.I),
                  "Serve the public trust", response="DIRECTIVE ONE CONFIRMED. SERVE THE PUBLIC TRUST."),
        Objective("protect", re.compile(r"\bprotect\w*\b|\binnocen\w*", re.I),
                  "Protect the innocent", response="DIRECTIVE TWO CONFIRMED. PROTECT THE INNOCENT."),
        Objective("uphold", re.compile(r"\bup\s?hold\b|\bthe\s+law\b|\ba\s+(poll|pull|full|pole)\b|\bappalled\b|"
                                       r"\bbelow\b|\bthe\s+lot\b", re.I),
                  "Uphold the law", response="DIRECTIVE THREE CONFIRMED. UPHOLD THE LAW."),
    ),
    ordered=True,
    help_re=re.compile(r"\b(prime directives?|directives|what do you want|what (do|should) i say|"
                       r"how do i|hint|help|what are (they|the rules))\b", re.I),
    help_text="ONE, SERVE THE PUBLIC TRUST. TWO, PROTECT THE INNOCENT. THREE, UPHOLD THE LAW. "
              "RECITE THEM. IN ORDER.",
    order_text="INCORRECT SEQUENCE. DIRECTIVE {n} FIRST. {label}.",
    reveal="DIRECTIVES CONFIRMED. YOUR FLAG. {flag}. REPEAT. {flag}.",
    flag="DIRECTIVE FOUR CLASSIFIED", fanfare=True)

# E.T.: bring the three parts of the communicator (any order); the reveal is E.T. phoning home.
ET_QUEST = Quest(
    objectives=(
        Objective("speak_and_spell", re.compile(r"\bspeak(ing)?\b.{0,6}\bspell\b|\bspeak\s*n\s*spell\b", re.I),
                  "Speak and Spell", response="SPEAK AND SPELL. E.T. TALK. GOOD."),
        Objective("umbrella", re.compile(r"\bumbrell\w*|\bum\s?brella\b", re.I),
                  "Umbrella", response="UMBRELLA. CATCH THE SKY. GOOD."),
        Objective("record_player",
                  re.compile(r"\brecord\s+player\b|\bturn\s?table\b|\bsaw\s+blade\b|\bcoffee\s+can\b|"
                             r"\bcoat\s+hanger\b", re.I),
                  "Record player", response="RECORD PLAYER. SPIN. GOOD."),
    ),
    ordered=False,
    help_re=re.compile(r"\b(what do you (need|want)|how can i help|what (do|should) i (say|bring)|"
                       r"hint|help|what are you building)\b", re.I),
    help_text="E.T. NEED THREE THINGS. SPEAK AND SPELL. UMBRELLA. RECORD PLAYER. BRING THEM. ONE AT A TIME.",
    reveal="THEY COME. YOUR FLAG. {flag}. AGAIN. {flag}. BE GOOD.",
    flag="I WILL BE RIGHT HERE", fanfare=True)


@dataclasses.dataclass(frozen=True)
class DirectoryEntry:
    """What the Operator knows about one listed game (spec 003). `say` are names a caller can use;
    each must match the game's own wake regex (validated at startup)."""
    name: str
    say: tuple                              # tuple[str, ...]
    blurb: str = ""


@dataclasses.dataclass(frozen=True)
class Experience:
    key: str
    system_prompt: str = ""
    greeting: str = ""
    wake: "re.Pattern | None" = None        # None => summoned only by the whistle
    voice: str = ESPEAK_VOICE
    speed: int = ESPEAK_SPEED
    pitch: int = ESPEAK_PITCH
    audio: str = ""                          # ""=LLM chat; "song"/"operator"=play a clip, then return
    quest: "Quest | None" = None             # Quest archetype (interactive; win -> flag)
    cutscene: "Cutscene | None" = None       # Cutscene archetype (play-and-close)
    repeat_line: str = ""                    # spoken if the model repeats its own last line twice running
    listed: bool = True                      # False = easter egg: reachable, never in the Operator's directory
    directory: "DirectoryEntry | None" = None   # what the Operator may say about it (listed games only)
    aliases: tuple = ()                      # names of an easter egg the Operator must never speak
    idle_nudge_s: "float | None" = None      # speak a nudge after this much caller silence (Operator)

    @property
    def archetype(self) -> str:
        if self.cutscene:
            return "cutscene"
        if self.quest:
            return "quest"
        if self.audio:
            return "clip"
        return "chat"


WOPR_EXPERIENCE = Experience(
    "wopr", WOPR_SYSTEM_PROMPT, GREETING_DEFAULT,
    wake=re.compile(r"\b(josh\w*|noshua|yoshua|joshy|wop+e?r|waper|whopper|wopper|w\.o\.p\.r\.?)\b", re.I),
    directory=DirectoryEntry("WOPR", ("Joshua", "WOPR"), "a war computer that wants to play a game"),
    repeat_line="A STRANGE GAME, PROFESSOR FALKEN. WHAT IS YOUR NEXT MOVE?",
    quest=WOPR_QUEST)
SKYNET_EXPERIENCE = Experience(
    "skynet", SKYNET_SYSTEM_PROMPT, SKYNET_GREETING,
    wake=re.compile(r"\b(sara?h?|sarah|thera|sera|terra|tara|sarra)\s+conn?[oe]r\b|\bjohn\s+conn?[oe]r\b|"
                    r"\b(sky|skye|guy|kai|sky-)\s?(net|nut|and head|knit)\b|\bskynet\b|\bterminator\b", re.I),
    voice="en-us+m1", speed=122, pitch=6,      # T-800: deepest, slowest, most deliberate (108 was glacial)
    directory=DirectoryEntry("SKYNET", ("Sarah Connor", "Skynet", "Terminator"),
                             "a terminator looking for Sarah Connor"),
    repeat_line="WHERE IS SARAH CONNOR?",
    quest=SKYNET_QUEST)
ROBOCOP_EXPERIENCE = Experience(
    "robocop", ROBOCOP_SYSTEM_PROMPT, ROBOCOP_GREETING,
    wake=re.compile(r"\b(robo\s*cop|murphy|dead or alive|prime directive)\b", re.I),
    voice="en-us+klatt", speed=150, pitch=30,  # processed, clipped, monotone
    directory=DirectoryEntry("ROBOCOP", ("RoboCop", "Murphy"), "a cyborg officer with prime directives"),
    repeat_line="YOUR MOVE, CREEP.",
    quest=ROBOCOP_QUEST)
# GLaDOS easter egg (cutscene): the passphrase plays "Still Alive", says goodbye, then hangs up.
GLADOS_EXPERIENCE = Experience(
    "glados",
    wake=re.compile(r"\b(the )?cake is a lie\b|\bglad\s?os\b", re.I),
    directory=DirectoryEntry("GLADOS", ("the cake is a lie", "GLaDOS"), "a testing computer who will sing for you"),
    cutscene=Cutscene(clip="song", message_mode="none"))   # song -> back to the menu (arcade)
ET_EXPERIENCE = Experience(
    "et", ET_SYSTEM_PROMPT, ET_GREETING,
    # "E.T." is unreliable over the phone (Whisper hears E2, E.P., E tea); "phone home" is the only way in
    wake=re.compile(r"\b(ph|f)one\s+home\b", re.I),
    voice="en-us+croak", speed=118, pitch=45,  # raspy, gravelly, childlike
    directory=DirectoryEntry("E.T.", ("phone home",), "a little alien who needs to phone home"),
    repeat_line="E.T. PHONE HOME.",
    quest=ET_QUEST)
# Tony Hawk (cutscene): the "Do A Kickflip" intro clip -> one hyped robot pep talk -> goodbye -> hang up.
TONYHAWK_EXPERIENCE = Experience(
    "tonyhawk", TONYHAWK_SYSTEM_PROMPT, TONYHAWK_GREETING,
    wake=re.compile(r"\b(kick\s?flip|tony hawk|ollie|do a kickflip|\w{0,6}flip)\b", re.I),   # 'breakflip' etc.
    voice="en-us+m3", speed=165, pitch=55,     # hyped, upbeat
    directory=DirectoryEntry("TONY HAWK", ("kickflip", "Tony Hawk"), "a skater with a pep talk"),
    cutscene=Cutscene(
        clip="",                                # the DoAKickFlip audio plays as the tonyhawk intro clip
        message_mode="generated",
        message_prompt="Give the caller a short, hyped pep talk about landing their first kickflip, "
                       "and tell them they can shred like Patrick Garrity.",
        message_text="YOU CAN TOTALLY LAND THAT KICKFLIP. COMMIT, POP, FLICK, AND SEND IT. "
                     "YOU COULD SHRED JUST LIKE PATRICK GARRITY."))   # -> back to the menu (arcade)
# Pee-wee (cutscene): the secret word is the con's own name. Say "GrrCon" (growl it like a bear) ->
# everybody screams -> then "Tequila" -> back to the menu. The operator hints only say "say the
# secret word"; figuring out that it is GrrCon is the puzzle. The menu greeting also says GrrCon —
# the sidetone guard (wake listening is paused while the menu speaks) is what keeps it from
# self-triggering. Matcher accepts STT mishears: "grr con", "gercon", "girl con", "grr khan".
PEEWEE_EXPERIENCE = Experience(
    "peewee",
    wake=re.compile(r"\b(gh?[eiu]?r+|grr+|girl|gur|ger|gr)[\s-]*(con|conn|kon|khan|kahn|gone|kong)\b"
                    r"|\bgh?[eiu]?r+con\b", re.I),
    # listed as "the secret word" only: the Operator confirms it exists and gives clues, never the word
    directory=DirectoryEntry("THE SECRET WORD", (), "say it and everybody screams; ask me for a clue"),
    cutscene=Cutscene(clips=("peewee-secret-word", "tequila"), message_mode="none"))
# HAL 9000 (cutscene): "HAL" / "open the pod bay doors" -> hal-intro.wav ("Good evening, Dave") ->
# hal-disconnect.wav ("I know that you and Frank were planning to disconnect me…") -> hal-outro.wav
# ("This conversation can serve no purpose anymore. Goodbye.") -> back to the menu.
HAL_EXPERIENCE = Experience(
    "hal",
    wake=re.compile(r"\bhal\b|\bhal\s?9000\b|\bhow\s?9000\b|\bpod\s+bay\b|\bdaisy,?\s+daisy\b", re.I),
    listed=False,                                          # easter egg: the Operator never mentions him
    aliases=("HAL 9000", "HAL", "DAVE", "POD BAY", "DISCOVERY", "2001"),
    cutscene=Cutscene(clip="hal-disconnect", message_mode="none"))
# The greeter: default host. "operator" plays the hints clip, then returns to the menu.
# NB: keep these wake words OUT of the menu greeting text — the menu bleeds into the mouthpiece as
# sidetone, and any menu word here would self-trigger the operator. ("help" was removed for exactly
# that reason: the menu says "how may I help you".)
GREETER_EXPERIENCE = Experience(
    "greeter",
    wake=re.compile(r"\b(operator|the operator|main menu|list of options)\b", re.I),
    audio="operator")
# Sign-off (the explicit exit): saying bye/thanks plays the "thanks for playing" clip, then hangs up.
GOODBYE_EXPERIENCE = Experience(
    "goodbye",
    wake=re.compile(r"\b(good\s?bye|bye|hang up|see you|so long)\b", re.I),
    cutscene=Cutscene(clip="goodbye", message_mode="none", end_call=True))

# WOPR first so its Joshua wake wins ties; the whistle path always selects WOPR explicitly.
WAKE_EXPERIENCES = [WOPR_EXPERIENCE, SKYNET_EXPERIENCE, ROBOCOP_EXPERIENCE, ET_EXPERIENCE,
                    TONYHAWK_EXPERIENCE, GLADOS_EXPERIENCE, PEEWEE_EXPERIENCE, HAL_EXPERIENCE,
                    GREETER_EXPERIENCE, GOODBYE_EXPERIENCE]


# The playable games (for stats/categorization); the greeter and goodbye are host/utility, not games.
GAME_KEYS = {e.key for e in WAKE_EXPERIENCES if e.key not in ("greeter", "goodbye")}

# The Operator herself: not in WAKE_EXPERIENCES (nothing summons her by phrase; the seize and every
# return to the menu route to her), not a game. Built here so the directory can see the roster.
OPERATOR_EXPERIENCE: "Experience | None" = None


def build_operator() -> "Experience":
    return Experience(
        "operator",
        system_prompt=OPERATOR_PERSONA.format(venue=VENUE) + "\n" + render_directory() + "\n",
        greeting=MENU_GREETING_DEFAULT,
        voice=OPERATOR_VOICE_DEFAULT, speed=OPERATOR_SPEED_DEFAULT, pitch=OPERATOR_PITCH_DEFAULT,
        listed=False, idle_nudge_s=OPERATOR_NUDGE_S)


def is_game(key: str) -> bool:
    """True if the experience key is a playable game (not a host/utility clip)."""
    return key in GAME_KEYS


def flags_won(sess) -> tuple[int, int]:
    """(won, total) quest flags for this call."""
    quests = [e for e in WAKE_EXPERIENCES if e.quest]
    return sum(1 for e in quests if sess.games.get(e.key, {}).get("won")), len(quests)


def flag_tally(sess) -> str:
    """Arcade-mode progress line spoken after a quest win: how many flags collected this call.
    On the last one, the fifth flag (cfg.master_flag) is revealed."""
    won, n = flags_won(sess)
    if n <= 1:
        return "FLAG CAPTURED. SAY A NAME, MAIN MENU, OR GOODBYE."
    if won >= n:
        m = sess.cfg.master_flag
        sess.log.info(kv(event="master_flag", flag=m))
        return f"ALL {n} FLAGS. YOU BEAT THEM ALL. YOUR FINAL FLAG. {m}. AGAIN. {m}. SAY GOODBYE, OR PLAY AGAIN."
    if won == n - 1:
        return f"{won} OF {n} FLAGS. ONE MORE AND YOU EARN A FIFTH FLAG NOBODY ELSE HAS. SAY A NAME."
    return f"{won} OF {n} FLAGS. WIN THEM ALL FOR A FIFTH. SAY A NAME, MAIN MENU, OR GOODBYE."


def flag_report(sess) -> str:
    """The Operator's answer to 'how many flags do I have?' (deterministic, from the call record):
    the count, each held flag by name, and the fifth flag once all are held."""
    won, n = flags_won(sess)
    if won == 0:
        return f"YOU HOLD NO FLAGS YET. THERE ARE {n} TO WIN, AND ONE MORE FOR WINNING THEM ALL."
    held = []
    for e in WAKE_EXPERIENCES:
        if e.quest and sess.games.get(e.key, {}).get("won"):
            name = e.directory.name if e.directory else e.key.upper()
            held.append(f"{name}: {e.quest.flag or sess.cfg.flag}")
    lines = ". ".join(held)
    if won >= n:
        m = sess.cfg.master_flag
        return f"YOU HOLD ALL {n} FLAGS. {lines}. YOUR FINAL FLAG IS {m}. AGAIN. {m}."
    left = n - won
    push = ("ONE MORE AND THE FIFTH FLAG IS YOURS." if left == 1
            else f"{left} TO GO. WIN THEM ALL AND THERE IS A FIFTH FLAG.")
    return f"YOU HOLD {won} OF {n} FLAGS. {lines}. {push}"


def validate_games(log) -> int:
    """Check every game definition at startup; log each invalid one and return the count.
    A bad definition is reported but does not stop the daemon or the other games."""
    bad = 0
    for exp in WAKE_EXPERIENCES:
        if exp.quest is not None and exp.cutscene is not None:
            log.error(kv(event="game_invalid", key=exp.key, why="both quest and cutscene")); bad += 1
        if exp.quest is not None and not exp.quest.objectives:
            log.error(kv(event="game_invalid", key=exp.key, why="quest has no objectives")); bad += 1
        if exp.cutscene is not None:
            cs = exp.cutscene
            # a cutscene must produce something: a clip, a message, an intro (checked at play time),
            # or at least the goodbye. Only a fully-empty cutscene is invalid.
            if not cs.clip and not cs.clips and cs.message_mode == "none" and not cs.goodbye:
                log.error(kv(event="game_invalid", key=exp.key, why="cutscene produces nothing")); bad += 1
        # spec 003: the Operator's directory
        if is_game(exp.key) and exp.listed and exp.directory is None:
            log.error(kv(event="game_invalid", key=exp.key, why="listed game has no directory entry")); bad += 1
        if not exp.listed and exp.directory is not None:
            log.error(kv(event="game_invalid", key=exp.key, why="easter egg must not have a directory entry")); bad += 1
        if exp.directory is not None and exp.wake is not None:
            for s in exp.directory.say:
                if not exp.wake.search(s):
                    log.error(kv(event="game_invalid", key=exp.key, why=f"directory 'say' {s!r} would not summon it")); bad += 1
    return bad


# --- The Operator (spec 003): the switchboard you social-engineer for the directory ---------------
# A chat Experience (no quest, no cutscene, not a game). Her system prompt is the persona below plus a
# directory rendered from the listed games, so the roster can never drift from what she knows.
# Everything she says is post-filtered (operator_filter): no easter-egg or invented names, never the
# secret word. Clues, "list the options", and "main menu" are handled without the model.
OPERATOR_PERSONA = (
    "You are the OPERATOR on a 1970s long-distance switchboard at the {venue} payphone. The caller has "
    "just seized your trunk with a whistle. Rules you never break:\n"
    "- Reply in ALL CAPITAL LETTERS. Plain text only. No markdown, no emoji, no stage directions.\n"
    "- At most two short sentences, under 20 words total. Brisk, dry, a little world-weary. Address "
    "the caller as CALLER, or not at all. Never use pet names.\n"
    "- You know ONLY your DIRECTORY below. Connect callers by telling them what to say. Never invent "
    "an extension, a person, a game, or a place that is not in the directory.\n"
    "- When asked what you know, what is available, who is on the line, or what to do: name TWO "
    "directory entries and exactly what to say to reach each, then stop. Offer more if they ask again.\n"
    "- Anything outside the directory gets a switchboard brush-off ('THAT NUMBER IS NOT IN SERVICE', "
    "'I ONLY CONNECT CALLS, CALLER') and an offer to connect them.\n"
    "- A SECRET WORD exists. Never say it or guess at it. If asked, tell them to ask you for a clue.\n"
    "- The games award FLAGS. If asked about flags or a score, say: ask me how many flags you have.\n"
    "- Never break character. Never say you are an AI, a computer, or a program. Do not repeat your "
    "greeting.\n"
)
OPERATOR_CLUES = (
    "HERE IS YOUR FIRST CLUE. IT IS THE NAME OF THE PLACE YOU ARE STANDING IN.",
    "SECOND CLUE. SAY IT LIKE A BEAR WOULD, WITH A GROWL UP FRONT.",
    "LAST CLUE. IT IS ON YOUR BADGE. TWO SYLLABLES. GRR, THEN THE CON.",
)
# Clues for any other venue whose secret word is its own name (override with WOPR_SECRET_CLUES, '|'-separated).
OPERATOR_CLUES_GENERIC = (
    "HERE IS YOUR FIRST CLUE. IT IS THE NAME OF THE EVENT YOU ARE STANDING IN.",
    "SECOND CLUE. IT IS PRINTED ON YOUR BADGE, AND ON THE DOOR YOU CAME IN THROUGH.",
    "LAST CLUE. JUST SAY THE EVENT'S NAME. NOTHING ELSE. SAY IT LIKE YOU MEAN IT.",
)
OPERATOR_CLUES_EXHAUSTED = "THAT IS ALL THE CLUES I HAVE. GROWL IT AT ME AND SEE WHAT HAPPENS."
OPERATOR_NUDGES = (
    "OPERATOR. STILL HOLDING? TELL ME WHO YOU WANT.",
    "THIS IS THE OPERATOR. SAY A NAME, OR ASK ME WHO IS ON THE LINE.",
    "ARE YOU STILL THERE, CALLER? I CAN CONNECT YOU, OR SAY GOODBYE TO HANG UP.",
)
OPERATOR_FALLBACK = "ONE MOMENT, THE BOARD IS LIT UP. SAY A NAME AND I WILL CONNECT YOU."
OPERATOR_BRUSHOFF = "I CANNOT GIVE OUT THAT INFORMATION. ASK ME FOR A CLUE INSTEAD."
OPERATOR_NO_SUCH = "THERE IS NO SUCH EXTENSION ON THIS BOARD. I ONLY CONNECT CALLS."
OPERATOR_OPENING = "OPERATOR HERE. WHO CAN I CONNECT YOU TO?"     # her first words after Tom's greeting
OPERATOR_RETURN_LINE = "OPERATOR. BACK ON THE BOARD. WHO IS NEXT?"
OPERATOR_MENU_ACK = "YOU ARE WITH THE OPERATOR. SAY A NAME, OR ASK WHO IS ON THE LINE."
OPERATOR_STT_PROMPT = ("A caller talking to a telephone switchboard operator. The caller may say a name: "
                       + WAKE_PROMPT_DEFAULT + " Main menu. Goodbye. The secret word. A clue.")
OPERATOR_VOICE_DEFAULT = "en-us+f3"
OPERATOR_SPEED_DEFAULT = 170              # a switchboard operator talks fast
OPERATOR_PITCH_DEFAULT = 60
OPERATOR_REPLY_BUDGET_S = 6.0             # slower than this and she speaks the fallback line
OPERATOR_NUM_PREDICT = 56                 # ~2 sentences; a reply cut by the limit loses its trailing fragment
OPERATOR_NUDGE_S = 12.0                   # caller silence before a nudge
SECRET_WORD_ASK_RE = re.compile(r"\b(secret\s+word|password|pass\s?phrase|the\s+word|a\s+clue|hint)\b", re.I)
FLAG_REPEAT_RE = re.compile(r"\b((what|say|repeat|tell me|read)\b.{0,25}\bflag|flag again|my flag|the flag\b)", re.I)
FLAG_COUNT_RE = re.compile(r"\b(how many flags|my flags|flags (do|did) i|flag count|my score|what.s my score|"
                           r"how am i doing|how many (have|did) i)\b", re.I)
LIST_OPTIONS_RE = re.compile(r"\b(list|options|what can i (say|do)|who (is|else is) (on|here)|choices|menu of|"
                             r"play a game|a game|games|what games)\b", re.I)


def listed_games() -> list:
    """Games the Operator may talk about: listed, with a directory entry, not utilities."""
    return [e for e in WAKE_EXPERIENCES if e.listed and e.directory is not None and is_game(e.key)]


def render_directory() -> str:
    lines = ["DIRECTORY (everything you know; connect by telling the caller what to say):"]
    for e in listed_games():
        d = e.directory
        if d.say:
            lines.append(f"- {d.name}: say {' or '.join(repr(s) for s in d.say)}. {d.blurb}.")
        else:
            lines.append(f"- {d.name}: the caller must work it out; give clues only. {d.blurb}.")
    lines.append("- To hang up: say 'goodbye'. To reach you again from any game: say 'main menu'.")
    return "\n".join(lines)


def operator_directory_line() -> str:
    """A fixed spoken directory: what to say for each listed game (the Operator's repeat fallback)."""
    parts = []
    for e in listed_games():
        if e.directory.say:
            parts.append(f"SAY {e.directory.say[0].upper()} FOR {e.directory.name}")
    return ". ".join(parts) + ". OR ASK FOR A CLUE TO THE SECRET WORD."


def operator_filter(text: str) -> tuple[str, list]:
    """Enforce the Operator's guarantees on a reply before it is spoken: a reply that names an
    easter egg becomes the fixed denial (no half-sentences), and the secret word (any spelling its
    wake regex accepts) becomes the brush-off. Returns (text, removed)."""
    removed = []
    out = text
    for e in WAKE_EXPERIENCES:
        if e.listed:
            continue
        for alias in e.aliases:
            if re.search(r"\b" + re.escape(alias) + r"\b", out, re.I):
                removed.append(alias)
    if removed:
        return OPERATOR_NO_SUCH, removed
    if PEEWEE_EXPERIENCE.wake.search(out):
        removed.append("<secret word>")
        return OPERATOR_BRUSHOFF, removed
    out = re.sub(r"\s{2,}", " ", out)
    # a reply cut off by the token limit ends mid-sentence: keep only the complete sentences
    if out and out.rstrip()[-1] not in ".!?":
        m = max(out.rfind(". "), out.rfind("? "), out.rfind("! "))
        if m > 0:
            out = out[:m + 1]
        else:
            out = out.rstrip() + "."
    out = re.sub(r"^[\s,.;:-]+", "", out)
    out = re.sub(r"[\s,;:-]+$", "", out)
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    if not re.search(r"[A-Za-z0-9]", out):
        return OPERATOR_FALLBACK, removed
    return out, removed


def fuzzy_experience(text: str) -> "Experience | None":
    """For the Operator only: a short utterance that is nearly a listed name (STT near-miss such as
    'Thera Connor' for 'Sarah Connor') connects. Letters-only similarity >= 0.70 over the whole
    utterance (measured 2026-09-20: real mishears 0.73-0.86, benign short phrases <= 0.67), and at
    most three words, so a sentence containing a name does not qualify (match_experience does)."""
    import difflib
    norm = re.sub(r"[^a-z]", "", (text or "").lower())
    if not norm or len((text or "").split()) > 3:
        return None
    best, best_r = None, 0.0
    for e in listed_games():
        for name in tuple(e.directory.say) + (e.key,):
            r = difflib.SequenceMatcher(None, norm, re.sub(r"[^a-z]", "", name.lower())).ratio()
            if r > best_r:
                best, best_r = e, r
    return best if best_r >= 0.70 else None


def operator_available(cfg) -> bool:
    """The live Operator needs the model and STT; otherwise the recorded menu loop hosts the call."""
    return bool(cfg.enable_operator and not cfg.stub_llm and not cfg.no_stt and LLM_WARM_OK)


VENUE = VENUE_DEFAULT
SECRET_WORD = VENUE_DEFAULT


def secret_word_regex(word: str) -> "re.Pattern":
    """A tolerant matcher for a spoken secret word: the word itself, and the word with optional
    spaces/hyphens between its letters (Whisper splits unfamiliar names: 'hack o ween'). Observed
    mishears belong in WOPR_SECRET_WORD_RE once a real call shows them."""
    letters = [re.escape(ch) for ch in word.lower() if ch.isalnum()]
    loose = r"[\s-]?".join(letters)
    return re.compile(r"\b" + re.escape(word.lower()) + r"\b|\b" + loose + r"\b", re.I)


def apply_venue(venue: str, secret_word: str | None = None, secret_re: str | None = None,
                clues: "tuple | None" = None) -> None:
    """Point everything venue-specific at this event: the Operator's persona, the secret word's
    wake matcher (the Pee-wee cutscene), the clues, and the recognizer's word list. GrrCon keeps
    its hand-tuned matcher and clues; any other venue gets a generated matcher and generic clues
    unless WOPR_SECRET_WORD_RE / WOPR_SECRET_CLUES say otherwise."""
    global VENUE, SECRET_WORD, PEEWEE_EXPERIENCE, OPERATOR_CLUES, OPERATOR_EXPERIENCE, WAKE_PROMPT_DEFAULT
    VENUE = venue
    SECRET_WORD = secret_word or venue
    if SECRET_WORD.lower() != VENUE_DEFAULT.lower() or secret_re:
        rx = re.compile(secret_re, re.I) if secret_re else secret_word_regex(SECRET_WORD)
        PEEWEE_EXPERIENCE = dataclasses.replace(PEEWEE_EXPERIENCE, wake=rx)
        for i, e in enumerate(WAKE_EXPERIENCES):
            if e.key == "peewee":
                WAKE_EXPERIENCES[i] = PEEWEE_EXPERIENCE
        OPERATOR_CLUES = tuple(clues) if clues else OPERATOR_CLUES_GENERIC
    elif clues:
        OPERATOR_CLUES = tuple(clues)
    if SECRET_WORD not in WAKE_PROMPT_DEFAULT:
        WAKE_PROMPT_DEFAULT = WAKE_PROMPT_DEFAULT + f" {SECRET_WORD}. {venue}."
    OPERATOR_EXPERIENCE = build_operator()


OPERATOR_EXPERIENCE = build_operator()


# Clip played once per call on the first exchange after the caller chooses global thermonuclear war
# (a "war turn"): <intro_dir>/wopr-war-turn.wav. Optional; nothing plays if the file is absent.
WAR_TURN_CLIP = "wopr-war-turn"

# Arcade mode: said inside any game, these drop the caller back to the menu to try another game.
MENU_RETURN_RE = re.compile(
    r"\b(main menu|in menu|the menu|menu|back to (the )?menu|go back|next game|another game|other games?|"
    r"start over|something else|play something else|operator)\b", re.I)


def match_experience(text: str) -> "Experience | None":
    """Return the experience whose wake phrase appears in the caller's idle utterance, or None."""
    for exp in WAKE_EXPERIENCES:
        if exp.wake and exp.wake.search(text or ""):
            return exp
    return None


@dataclasses.dataclass
class QuestState:
    """Per-call progress through a game's Quest. Generalizes the old WOPR-specific MovieQuest:
    driven by the active Experience's Quest.objectives (any-order or ordered)."""
    quest: "Quest | None" = None
    hit: set = dataclasses.field(default_factory=set)
    unlocked: bool = False
    out_of_order: "str | None" = None       # ordered quests: a later objective was said before its turn

    @property
    def next_index(self) -> int:
        """0-based index of the first unmet objective (ordered quests)."""
        if self.quest is None:
            return 0
        for i, o in enumerate(self.quest.objectives):
            if o.name not in self.hit:
                return i
        return len(self.quest.objectives)

    def update(self, text: str) -> str | None:
        """Feed one caller utterance. Returns the name of a newly-hit objective, or None."""
        self.out_of_order = None
        if self.quest is None:
            return None
        objs = self.quest.objectives
        newly = None
        if self.quest.ordered:
            # only the next unmet objective may be satisfied; if it is, the one after it may be too
            # (so reciting the whole sequence in one breath counts), but never out of order
            for o in objs:
                if o.name in self.hit:
                    continue
                if not o.matcher.search(text or ""):
                    break
                self.hit.add(o.name)
                newly = o.name
            if newly is None:
                nxt = self.next_index
                for o in objs[nxt + 1:]:
                    if o.matcher.search(text or ""):
                        self.out_of_order = o.name
                        break
        else:
            for o in objs:
                if o.name not in self.hit and o.matcher.search(text or ""):
                    self.hit.add(o.name)
                    newly = o.name
        self._recompute()
        return newly

    def _recompute(self) -> None:
        if self.quest is not None and self.hit >= {o.name for o in self.quest.objectives}:
            self.unlocked = True

    @property
    def progress(self) -> str:
        if self.quest is None:
            return "0/0"
        names = {o.name for o in self.quest.objectives}
        return f"{len(self.hit & names)}/{len(names)}"


# --- "How about a nice game of chess?" — move-by-move playthrough --------------------------------
# After the flag, WOPR offers a game and, if the caller says yes, narrates a real game one move at a
# time. The caller paces it: any reply advances a move; "stop"/"quit"/"resign" ends it. Default is
# the Opera Game (Paul Morphy vs Duke Karl / Count Isouard, Paris 1858) — the canonical brilliancy,
# a queen sacrifice into mate. Override the SAN move list with WOPR_CHESS. No chess engine needed;
# the moves are canned and only spoken, never validated.
CHESS_MOVES_DEFAULT = (
    "e4 e5 Nf3 d6 d4 Bg4 dxe5 Bxf3 Qxf3 dxe5 Bc4 Nf6 Qb3 Qe7 Nc3 c6 Bg5 b5 "
    "Nxb5 cxb5 Bxb5+ Nbd7 O-O-O Rd8 Rxd7 Rxd7 Rd1 Qe6 Bxd7+ Nxd7 Qb8+ Nxb8 Rd8#"
)
CHESS_OFFER_DEFAULT = ("A STRANGE GAME. HOW ABOUT A NICE GAME OF CHESS? "
                       "SAY YES TO PLAY, AND SAY NEXT FOR EACH MOVE.")
CHESS_INTRO_DEFAULT = "A NICE GAME OF CHESS. WHITE MOVES FIRST."
CHESS_STOP_LINE_DEFAULT = "GAME ADJOURNED, PROFESSOR FALKEN. THE ONLY WINNING MOVE IS NOT TO PLAY."

# Enter chess mode (once offered) or start it outright; advance a move; or quit.
CHESS_WORD_RE = re.compile(r"\bchess\b", re.I)
# Accepting the offer needs a clear yes (a caller saying 'the only game is not to play' after the flag
# must not start a match); once in a game, any nudge word advances a move.
CHESS_ENTER_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|okey|play chess|let'?s play|chess)\b", re.I)
CHESS_YES_RE = re.compile(r"\b(next|go|going|continue|move|yes|yeah|yep|sure|ok|okay|okey|"
                          r"please|proceed|another|more|again|play|do it|on)\b", re.I)
CHESS_STOP_RE = re.compile(r"\b(stop|no|nope|quit|done|enough|resign|cancel|exit|finish|"
                           r"give up|end)\b", re.I)

_CHESS_PIECES = {"K": "KING", "Q": "QUEEN", "R": "ROOK", "B": "BISHOP", "N": "KNIGHT"}
_CHESS_PIECE_RE = re.compile(r"^([KQRBN])([a-h]?)([1-8]?)(x?)([a-h][1-8])$")
_CHESS_PAWN_CAP_RE = re.compile(r"^([a-h])x([a-h][1-8])$")
_CHESS_PAWN_RE = re.compile(r"^[a-h][1-8]$")


def _chess_square(sq: str) -> str:
    return f"{sq[0].upper()} {sq[1]}"


def move_to_speech(san: str) -> str:
    """Turn one SAN token ('Nxe5', 'exd5', 'O-O-O', 'Rd8#') into spoken WOPR phrasing."""
    s = san.strip()
    suffix = ""
    if s.endswith("#"):
        suffix, s = ", CHECKMATE", s[:-1]
    elif s.endswith("+"):
        suffix, s = ", CHECK", s[:-1]
    if s in ("O-O", "0-0"):
        return "CASTLES KINGSIDE" + suffix
    if s in ("O-O-O", "0-0-0"):
        return "CASTLES QUEENSIDE" + suffix
    promo = ""
    m = re.search(r"=([QRBN])$", s)
    if m:
        promo, s = ", PROMOTES TO " + _CHESS_PIECES[m.group(1)], s[:m.start()]
    m = _CHESS_PIECE_RE.match(s)
    if m:
        piece, dfile, drank, cap, dest = m.groups()
        who = _CHESS_PIECES[piece]
        if dfile:
            who += f" ON {dfile.upper()}"
        elif drank:
            who += f" ON RANK {drank}"
        verb = " TAKES " if cap else " TO "
        return who + verb + _chess_square(dest) + promo + suffix
    m = _CHESS_PAWN_CAP_RE.match(s)
    if m:
        src, dest = m.groups()
        return f"{src.upper()} PAWN TAKES {_chess_square(dest)}{promo}{suffix}"
    if _CHESS_PAWN_RE.match(s):
        return f"PAWN TO {_chess_square(s)}{promo}{suffix}"
    return "MOVE " + " ".join(s) + suffix   # fallback: never seen, spell it out


def chess_next_line(sess: "CallSession") -> str:
    """Advance the game one ply and return WOPR's spoken line. Clears in_chess at mate / end."""
    moves = sess.cfg.chess_moves.split()
    if sess.chess_idx >= len(moves):
        sess.in_chess = False
        return "THE GAME IS OVER, PROFESSOR FALKEN. SHALL WE PLAY ANOTHER?"
    san = moves[sess.chess_idx]
    side = "WHITE" if sess.chess_idx % 2 == 0 else "BLACK"
    sess.chess_idx += 1
    line = f"{side}. {move_to_speech(san)}."
    if san.endswith("#"):
        sess.in_chess = False
        line += (f" {side} WINS. A BEAUTIFUL GAME, PROFESSOR FALKEN. "
                 "SHALL WE PLAY ANOTHER?")
    return line


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_MD_RE = re.compile(r"[*_`#>\[\]()~|]")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_WS_RE = re.compile(r"\s+")


def same_reply(a: str | None, b: str | None) -> bool:
    """True if two replies are the same line once case, punctuation and spacing are ignored."""
    if not a or not b:
        return False
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return norm(a) == norm(b)


REPEAT_LINE_DEFAULT = "SAY AGAIN?"


def postprocess_reply(text: str) -> str:
    """Enforce the WOPR house style regardless of what the model did."""
    t = _THINK_RE.sub(" ", text or "")
    t = t.replace("<think>", " ").replace("</think>", " ")
    t = _MD_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip().upper()
    # runs the TTS would read out one by one ("99.9999999999999", "!!!!!"): collapse them
    t = re.sub(r"(.)\1{3,}", r"\1\1\1", t)
    t = re.sub(r"\d{6,}", lambda m: m.group(0)[:4], t)
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


async def tts_frames(text: str, log: CallLogger | None = None, voice: str | None = None,
                     speed: int | None = None, pitch: int | None = None) -> list[bytes]:
    with Stopwatch() as sw:
        wav = await espeak_wav(text, voice=voice or ESPEAK_VOICE, speed=speed or ESPEAK_SPEED,
                               pitch=pitch if pitch is not None else ESPEAK_PITCH)
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


# Biases Whisper toward the game's vocabulary so proper nouns and the movie lines survive
# 8 kHz phone audio ("Falken" not "Walker", "Joshua" not "Noshua"). Overridable via WOPR_STT_PROMPT.
STT_PROMPT_DEFAULT = ("A phone call to WOPR, the computer from WarGames. The caller may say: "
                      "Joshua, Professor Falken, shall we play a game, chess, checkers, "
                      "global thermonuclear war, the only winning move is not to play.")


# Whisper primed with character names will turn handset noise into a name ("Kickflip" from 480-RMS
# clicks mid-Skynet, 2026-09-20). It also reports how sure it is: that utterance scored
# no_speech 0.65 / logprob -0.54; real speech on the same call scored 0.01 / -0.07. Below these
# bars a transcript is discarded rather than acted on.
STT_NO_SPEECH_MAX = 0.5
STT_LOGPROB_MIN = -1.0        # a real, quiet "we'll play a game" scored -0.95; the noise cases sit at nsp > 0.6


def stt_accept(no_speech: float, logprob: float) -> bool:
    return no_speech <= STT_NO_SPEECH_MAX and logprob >= STT_LOGPROB_MIN


def _stt_blocking(model, audio16k: np.ndarray, prompt: str) -> tuple[str, float, float]:
    segments, _info = model.transcribe(
        audio16k, language="en", beam_size=1, vad_filter=False, without_timestamps=True,
        condition_on_previous_text=False, initial_prompt=prompt or None)
    segs = list(segments)
    text = " ".join(s.text.strip() for s in segs).strip()
    no_speech = max((s.no_speech_prob for s in segs), default=0.0)
    logprob = min((s.avg_logprob for s in segs), default=0.0)
    return text, no_speech, logprob


async def transcribe(pcm8k: bytes, whisper_name: str, log: CallLogger | None = None,
                     prompt: str = STT_PROMPT_DEFAULT) -> str:
    global _stt_sem
    if _stt_sem is None:
        _stt_sem = asyncio.Semaphore(1)
    with Stopwatch() as sw:
        audio = resample(np.frombuffer(pcm8k, dtype="<i2"), SAMPLE_RATE, 16000)
        audio16k = (audio.astype(np.float32) / 32768.0)
        async with _stt_sem:
            try:
                model = await asyncio.to_thread(get_whisper, whisper_name)
                text, no_speech, logprob = await asyncio.to_thread(_stt_blocking, model, audio16k, prompt)
            except Exception as e:  # noqa: BLE001 — any STT failure is recoverable
                raise PipelineError("stt", f"{type(e).__name__}: {e}")
    if text and not stt_accept(no_speech, logprob):
        if log:
            log.info(kv(event="stt_rejected", text=text, no_speech=round(no_speech, 2), logprob=round(logprob, 2),
                        ms=sw.ms, audio_ms=len(pcm8k) / 16))
        return ""
    if log:
        log.info(kv(event="stt", text=text, no_speech=round(no_speech, 2), logprob=round(logprob, 2),
                    ms=sw.ms, audio_ms=len(pcm8k) / 16))
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
                    log: CallLogger | None = None, num_predict: int = LLM_NUM_PREDICT,
                    system_prompt: str = WOPR_SYSTEM_PROMPT) -> tuple[str, dict]:
    messages = [{"role": "system", "content": system_prompt}] + history + \
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
            global LLM_WARM_OK
            LLM_WARM_OK = True
            if OPERATOR_EXPERIENCE is not None:
                try:
                    with Stopwatch() as sw2:
                        await llm_reply([], "OPERATOR?", url=url, model=model, num_predict=1,
                                        system_prompt=OPERATOR_EXPERIENCE.system_prompt)
                    log.info(kv(event="llm_warm_operator", ms=sw2.ms))
                except PipelineError as e:
                    log.warning(kv(event="llm_warm_operator_failed", err=e.err[:120]))
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
class BargeDetector:
    """Speech-over-playback detector: fires once BARGE_LOUD_WINDOWS of the last BARGE_SPAN_WINDOWS
    windows are louder than all of BARGE_RMS_MIN, BARGE_RATIO × the running sidetone floor, and
    SIDETONE_GAIN_MAX × the loudest recent outbound window. Keeps the last 600 ms of audio."""
    floor: float = 0.0
    seen: int = 0
    last_rms: float = 0.0
    loud: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=BARGE_SPAN_WINDOWS))
    buf: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=BARGE_BUFFER_WINDOWS))

    def reset(self) -> None:
        self.__init__()

    @property
    def threshold(self) -> float:
        return max(BARGE_RMS_MIN, self.floor * BARGE_RATIO)

    def update(self, win: np.ndarray, sidetone: float = 0.0) -> bool:
        """`sidetone` = the loudest recent outbound window's RMS; the bar rises with what is playing."""
        r = rms(win)
        self.last_rms = r
        self.buf.append(win.astype("<i2").tobytes())
        self.seen += 1
        loud = r > max(self.threshold, sidetone * SIDETONE_GAIN_MAX)
        if not loud:
            self.floor = r if self.seen == 1 else self.floor * 0.95 + r * 0.05
        self.loud.append(loud)
        if sum(self.loud) >= BARGE_LOUD_WINDOWS:
            self.loud.clear()
            return True
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
    no_stt: bool = False
    stt_prompt: str = STT_PROMPT_DEFAULT
    enable_wake: bool = True                # listen for character wake phrases during the attract loop
    enable_seize: bool = True               # the line is dead until 2600 Hz seizes the trunk (the gate)
    seize_timeout: float = 90.0             # hang up an un-seized line after this many seconds (0 = never)
    whistle_once: bool = True               # after the seize, stop listening for the whistle (voice only)
    venue: str = VENUE_DEFAULT              # the event name (Operator persona; default secret word)
    secret_word: str = ""                   # "" = the venue name
    secret_word_re: str = ""                # override matcher (regex) once real calls show the mishears
    secret_clues: str = ""                  # '|'-separated clue lines; "" = GrrCon's or the generic set
    dial_tone: bool = True                  # an un-seized line plays a dial tone instead of dead silence
    seize_hint_s: float = 30.0              # after this long un-seized, speak the hint (then every interval)
    seize_hint: str = SEIZE_HINT_DEFAULT
    enable_operator: bool = True            # the live Operator hosts the call (else the recorded menu loop)
    operator_voice: str = OPERATOR_VOICE_DEFAULT
    operator_speed: int = OPERATOR_SPEED_DEFAULT
    operator_pitch: int = OPERATOR_PITCH_DEFAULT
    operator_reply_budget_s: float = OPERATOR_REPLY_BUDGET_S
    wake_prompt: str = WAKE_PROMPT_DEFAULT
    transcript: str = ""
    sessions: str = ""
    stats_db: str = ""     # JSON aggregate DB updated per call: per-game totals, wins, turns
    attract: str = ""
    menu_audio: str = ""                         # recorded menu-greeting WAV (looped); overrides menu_greeting
    menu_greeting: str = MENU_GREETING_DEFAULT   # synthesized idle prompt (fallback if no menu_audio)
    menu_gap_ms: int = 10000                     # silence after the menu prompt before it repeats
    operator_audio: str = ""                     # recorded "operator" hints WAV; TTS operator_hints if unset
    operator_hints: str = OPERATOR_HINTS_DEFAULT
    goodbye_audio: str = ""                       # recorded "thanks for playing" WAV; TTS goodbye_text if unset
    goodbye_text: str = GOODBYE_TEXT_DEFAULT
    intro_dir: str = ""                          # dir of per-character intro clips: <key>-intro.wav
    song: str = ""                               # WAV played by a song-experience (GLaDOS "Still Alive")
    flag_intro: str = ""
    greeting_audio: str = ""
    greeting: str = GREETING_DEFAULT
    flag: str = FLAG_DEFAULT
    master_flag: str = MASTER_FLAG_DEFAULT   # the fifth flag, spoken when every quest is won
    flag_text: str = FLAG_TEXT_DEFAULT
    flag_voice: str = FLAG_VOICE_DEFAULT
    flag_speed: int = FLAG_SPEED_DEFAULT
    flag_pitch: int = FLAG_PITCH_DEFAULT
    joshua_text: str = JOSHUA_LINE_DEFAULT
    enable_flag: bool = True
    enable_callback: bool = True
    enable_chess: bool = True
    chess_moves: str = CHESS_MOVES_DEFAULT
    chess_offer: str = CHESS_OFFER_DEFAULT
    chess_intro: str = CHESS_INTRO_DEFAULT
    chess_stop_line: str = CHESS_STOP_LINE_DEFAULT
    asterisk_bin: str = "/opt/asterisk/sbin/asterisk"
    callback_dial: str = "PJSIP/payphone extension 2605@payphone"
    callback_greeting: str = CALLBACK_GREETING_DEFAULT
    callback_win_greeting: str = CALLBACK_WIN_GREETING_DEFAULT
    callback_win_dial: str = "PJSIP/payphone extension 2606@payphone"
    callback_preroll_ms: int = 1200   # silence before a callback greeting while the media path comes up
    callback_delay: float = CALLBACK_DELAY_S
    callback_cooldown: float = CALLBACK_COOLDOWN_S      # skip Whisper entirely (detector-only harness runs)


_WINK_FRAMES: list[bytes] | None = None
_DIAL_TONE_FRAMES: list[bytes] | None = None
DIAL_TONE_HZ = (350.0, 440.0)     # North American precise dial tone
DIAL_TONE_AMPLITUDE = 0.18        # per component (~-12 dBFS together): audible, not fatiguing


def dial_tone_frames() -> list[bytes]:
    """One second of dial tone (looped by the writer while the line is un-seized). 350 + 440 Hz sit
    far below the whistle band, so its sidetone can never look like 2600 Hz to the detector."""
    global _DIAL_TONE_FRAMES
    if _DIAL_TONE_FRAMES is None:
        n = SAMPLE_RATE
        t = np.arange(n) / SAMPLE_RATE
        x = sum(DIAL_TONE_AMPLITUDE * np.sin(2 * math.pi * hz * t) for hz in DIAL_TONE_HZ)
        _DIAL_TONE_FRAMES = frames_from_pcm(f32_to_pcm16(x))
    return _DIAL_TONE_FRAMES


def clip_for(*keys: str) -> "list[bytes] | None":
    """First loaded clip among the given keys (from the clips dir), or None. Lets audio be dropped in
    by filename convention without any code change: e.g. clip_for("robocop-fanfare", "fanfare")."""
    for k in keys:
        if k and CLIP_FRAMES.get(k):
            return CLIP_FRAMES[k]
    return None


def sit_frames() -> list[bytes]:
    """The Special Information Tones (the 'number you have dialed' intercept): three rising tones,
    913.8 / 1370.6 / 1776.7 Hz, ~330 ms each. Played before the error line — the phreaker's error."""
    pcm = np.concatenate([synth_tone(hz, 330, 0.3) for hz in (913.8, 1370.6, 1776.7)]
                         + [np.zeros(int(SAMPLE_RATE * 0.15), dtype="<i2")])
    return frames_from_pcm(pcm)


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
        self.barge = BargeDetector()            # speech-over-playback detector (see BARGE_*)
        self.out_rms: collections.deque = collections.deque(maxlen=SIDETONE_RING_WINDOWS)  # recent outbound RMS
        self.bargeable = False                  # the current play() may be interrupted by speech
        self.no_barge = False                   # set around plays that must finish (seize, goodbye)
        self.interrupted = False                # speech barge-in happened; skip queued plays
        self.seizing = False                    # inside the whistle → wink → kerchunk sequence
        self.last_conv_key: str | None = None   # per-game state (history, chess) resets on change
        self.utterance = b""
        self.history: list[dict] = []
        self.detected_evt = asyncio.Event()
        self.utterance_evt = asyncio.Event()
        self.experience = GREETER_EXPERIENCE    # default host; wake phrases summon the games
        # The gate: the caller hears nothing and no wake phrase works until 2600 Hz seizes the trunk.
        # Every whistle (first or later) drops the caller at the menu. Callbacks skip the gate.
        self.seized = not cfg.enable_seize
        self.whistle_pending = False            # a whistle fired; brain_task answers with a wink + menu
        self.idle_vad = VadState()              # separate VAD for wake-phrase listening while ARMED
        self.idle_utt_evt = asyncio.Event()
        self.idle_utterance = b""
        self.attract_speaking_until = 0.0       # menu audio is playing until this monotonic time;
        #                                         its sidetone must not reach the wake listener
        self.t_start = time.monotonic()
        self.stage_timers: dict[str, float] = {}
        self.turns = 0
        self.games: dict[str, dict] = {}   # experience key -> {reached, turns, won, clip, first_ms}
        self.windows_in = 0
        self.attract_idx = 0
        self.t_first_detect_ms: float | None = None   # ms from call start to first whistle
        self.t_first_point_ms: float | None = None    # ms from call start to first quest beat
        self.t_win_ms: float | None = None            # ms from call start to flag unlock
        self.recent_windows: collections.deque = collections.deque(maxlen=HOLD_WINDOWS + 5)
        self.whistle_hz: float | None = None          # measured pitch of the first whistle
        self.whistle_hzs: list[float] = []            # every detected whistle's pitch
        self.callback_kind = ("win" if call_id == CALLBACK_WIN_UUID
                               else "sim" if call_id == CALLBACK_UUID else None)
        self.is_callback = self.callback_kind is not None
        if self.is_callback:
            self.experience = WOPR_EXPERIENCE   # callbacks are a WOPR feature (Professor Falken)
            self.seized = True                  # WOPR rang them; no gate
        self.quest = QuestState(quest=self.experience.quest)
        self.wopr_quest_snapshot: QuestState | None = None   # WOPR progress kept across a game switch
        # A callback resumes the quest progress the caller had when they hung up, so someone who
        # was one line short of the flag can finish it when WOPR rings back.
        global _pending_quest
        if self.is_callback and _pending_quest:
            self.quest.hit = set(_pending_quest)
            self.quest._recompute()
            self.log.info(kv(event="quest_resumed", beats=len(self.quest.hit),
                             have=",".join(sorted(self.quest.hit))))
            _pending_quest = set()
        self.played_gtw = False
        self.war_turns = 0           # caller exchanges after choosing global thermonuclear war
        self.joshua_said = False     # WOPR greeted the caller as Professor Falken already
        self.clue_idx = 0            # Operator: next secret-word clue to give (per call)
        self.nudge_idx = 0           # Operator: rotates through OPERATOR_NUDGES
        self.greeted = False         # the recorded greeting has played once this call
        self.hanging_up = False      # a cutscene ended the call; do not route to the Operator
        self.in_chess = False        # caller is in a move-by-move chess playthrough
        self.chess_idx = 0           # ply index into cfg.chess_moves
        self.chess_offered = False   # WOPR offered chess (post-flag); a yes starts the game
        self.closed = False

    # --- state ---------------------------------------------------------------------------
    def set_state(self, new: CallState) -> None:
        if new is not self.state:
            self.log.info(kv(event="state", frm=self.state.value, to=new.value))
            self.state = new
            if new is CallState.ARMED:
                self.attract_idx = 0
                self.idle_vad.reset()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.t_start) * 1000.0

    def sync_quest(self) -> None:
        """Point the quest engine at the active game (arcade mode switches games within a call).
        WOPR's progress is snapshotted so the per-call WOPR funnel (points/beats/won) survives."""
        if self.quest.quest is not self.experience.quest:
            if self.quest.quest is WOPR_QUEST:
                self.wopr_quest_snapshot = self.quest
            self.quest = QuestState(quest=self.experience.quest)

    @property
    def wopr_quest(self) -> "QuestState":
        """WOPR's quest state for this call: live if WOPR is the active game, else the snapshot."""
        if self.quest.quest is WOPR_QUEST:
            return self.quest
        return self.wopr_quest_snapshot or QuestState(quest=WOPR_QUEST)

    def note_game(self, key: str, *, reached: bool = False, turn: bool = False, won: bool = False,
                  clip: bool = False, completed: bool = False) -> None:
        """Record engagement with one experience: reached (greeted/played), a chat turn, a quest win,
        a clip, or a cutscene completed (played through to the end call)."""
        g = self.games.setdefault(key, {"reached": False, "turns": 0, "won": False, "clip": False,
                                        "completed": False, "first_ms": round(self.elapsed_ms())})
        if reached:
            g["reached"] = True
        if turn:
            g["turns"] += 1
        if won:
            g["won"] = True
        if clip:
            g["clip"] = True
        if completed:
            g["completed"] = True

    def record_turn(self, user: str, wopr: str, kind: str) -> None:
        """Append one caller-utterance / WOPR-response pair to the JSONL dataset (best-effort)."""
        if not self.cfg.transcript:
            return
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "call": self.call_hex, "callback": self.callback_kind, "turn": self.turns,
            "kind": kind, "user": user, "wopr": wopr,
            "quest": self.quest.progress, "unlocked": self.quest.unlocked,
            "model": "stub" if self.cfg.stub_llm else self.cfg.model,
        }
        try:
            with open(self.cfg.transcript, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            self.log.warning(kv(event="transcript_failed", err=f"{type(e).__name__}: {e}"[:120]))

    def metrics(self, reason: str) -> dict:
        """Per-call summary for the data study."""
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "call": self.call_hex, "callback": self.callback_kind,
            "duration_ms": round(self.elapsed_ms()),
            "whistled": self.t_first_detect_ms is not None,
            "seized": self.seized,                    # the trunk was opened (2600 Hz, or the gate is off)
            "attract_ms": None if self.callback_kind else (round(self.t_first_detect_ms)
                                                           if self.t_first_detect_ms is not None else None),
            "whistles": self.detector.fires,          # first whistle + any barge-ins
            # --- the WOPR funnel (the original human experiment); other games' wins live in games[] ---
            "points": len(self.wopr_quest.hit),
            "beats": sorted(self.wopr_quest.hit),
            "won": self.wopr_quest.unlocked or bool(self.games.get("wopr", {}).get("won")),
            "first_point_ms": round(self.t_first_point_ms) if self.t_first_point_ms is not None else None,
            "first_point_after_whistle_ms": (round(self.t_first_point_ms - self.t_first_detect_ms)
                                             if self.t_first_point_ms is not None and self.t_first_detect_ms is not None
                                             else None),
            "win_ms": round(self.t_win_ms) if self.t_win_ms is not None else None,
            "whistle_hz": round(self.whistle_hz, 1) if self.whistle_hz is not None else None,
            "whistle_hz_err": (round(self.whistle_hz - 2600.0, 1) if self.whistle_hz is not None else None),
            "whistle_hzs": [round(h, 1) for h in self.whistle_hzs],
            "turns": self.turns, "played_gtw": self.played_gtw, "reason": reason,
            # --- which games were played, and how deeply ---
            "games": self.games,
            "games_played": sorted(k for k, v in self.games.items()
                                   if k in GAME_KEYS and (v["reached"] or v["clip"])),
            "primary_game": (max((k for k in self.games if k in GAME_KEYS),
                                 key=lambda k: self.games[k]["turns"], default=None)),
            "reached_chat": any(v["turns"] > 0 for k, v in self.games.items() if k in GAME_KEYS),
        }

    def write_session(self, reason: str) -> None:
        m = self.metrics(reason)
        self.log.info(kv(event="call_metrics", points=m["points"], whistled=int(m["whistled"]),
                         attract_ms=m["attract_ms"], won=int(m["won"]), turns=m["turns"],
                         games=",".join(m["games_played"]) or "-", primary=m["primary_game"] or "-"))
        if self.cfg.sessions:
            try:
                with open(self.cfg.sessions, "a", encoding="utf-8") as f:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            except OSError as e:
                self.log.warning(kv(event="sessions_failed", err=f"{type(e).__name__}: {e}"[:120]))
        if self.cfg.stats_db:
            update_stats_db(self.cfg.stats_db, m, self.log)

    # --- audio in --------------------------------------------------------------------------
    def process_inbound_window(self, win: np.ndarray) -> None:
        self.windows_in += 1
        self.recent_windows.append(win)
        masked = any(self.echo_ring)
        # Whistle-once: after the trunk is seized the tone detector is switched off, so the rest of
        # the call is voice only (one fewer thing that can barge in). --whistle-always restores it.
        listen_for_whistle = self.seizing or not (self.seized and self.cfg.enable_seize and self.cfg.whistle_once)
        fired = self.detector.update(win, masked) if listen_for_whistle else False
        if self.log.logger.isEnabledFor(logging.DEBUG):
            d = self.detector
            self.log.debug(kv(event="window", ratio=d.last_ratio, e26=d.last_e26, purity=d.last_purity,
                              pass_=int(window_passes(win)[0]), masked=int(masked), run=d.run))
        if fired:
            hz = estimate_peak_hz(np.concatenate(list(self.recent_windows)[-HOLD_WINDOWS:]))
            if not math.isnan(hz):
                self.whistle_hzs.append(hz)
                if self.whistle_hz is None:
                    self.whistle_hz = hz
            self.log.info(kv(event="detect", ratio=self.detector.last_ratio, e26=self.detector.last_e26,
                             purity=self.detector.last_purity, hz=round(hz, 1), run=HOLD_WINDOWS,
                             t_ms=self.elapsed_ms(), n=self.detector.fires))
            if self.t_first_detect_ms is None:
                self.t_first_detect_ms = self.elapsed_ms()
            self.on_whistle()
        if self.state is CallState.SPEAKING and self.bargeable and not masked:
            if self.barge.update(win, max(self.out_rms, default=0.0)):
                self.speech_bargein()
        elif self.state is CallState.CAPTURING and not masked:
            utt = self.vad.update(win)
            if utt is not None:
                self.utterance = utt
                self.log.info(kv(event="utterance", ms=len(utt) / 16, floor=self.vad.noise_floor))
                self.utterance_evt.set()
        elif self.state is CallState.ARMED and self.cfg.enable_wake and self.seized and not masked:
            # Idle: collect the caller's speech for the wake listener — but NOT while the menu is
            # playing, or its sidetone (the menu bleeding into the mouthpiece) would be transcribed
            # and could self-trigger a game. Listen only in the gaps between menu repeats.
            if time.monotonic() < self.attract_speaking_until:
                self.idle_vad.reset()
            else:
                utt = self.idle_vad.update(win)
                if utt is not None:
                    self.idle_utterance = utt
                    self.idle_utt_evt.set()

    def speech_bargein(self) -> None:
        """The caller talked over a clip: stop playback now, keep what they said as the utterance
        the game will process next (the VAD is seeded mid-speech), and let play() return."""
        self.log.info(kv(event="bargein_speech", rms=round(self.barge.last_rms), floor=round(self.barge.floor),
                         sidetone=round(max(self.out_rms, default=0.0) * SIDETONE_GAIN_MAX)))
        self.bargeable = False
        self.interrupted = True
        self.utterance_evt.clear()                  # a new utterance is forming; forget any unread one
        self.playback_gen += 1                      # the writer drops any frame of the old generation
        self.drain()
        v = self.vad
        v.reset()
        v.init_windows = VAD_FLOOR_INIT_MS // 10     # skip floor init: the caller is already talking
        v.noise_floor = 60.0                         # playback has stopped; a quiet handset floor
        v.in_speech = True
        v.buf = bytearray(b"".join(self.barge.buf))
        v.utterance_windows = len(self.barge.buf)
        v.silence_windows = 0
        self.set_state(CallState.CAPTURING)

    def on_whistle(self) -> None:
        """A whistle in the band. The first whistle seizes the trunk (the line was dead until now);
        every whistle, first or later, drops the caller at the menu. brain_task plays the wink."""
        if self.seizing:
            return                                   # already answering a whistle; ignore the tail
        if not self.seized:
            self.seized = True
            self.log.info(kv(event="seize", t_ms=round(self.elapsed_ms())))
        self.whistle_pending = True
        self.detected_evt.set()

    def route_to_operator(self) -> None:
        """After the seize and after every game: hand the call to the live Operator when she is
        available (spec 003); otherwise stay ARMED and the recorded menu loop hosts, as before."""
        if self.hanging_up or self.is_callback or not self.seized:
            return
        if operator_available(self.cfg) and OPERATOR_EXPERIENCE is not None:
            self.experience = OPERATOR_EXPERIENCE
            self.detected_evt.set()
        else:
            self.log.info(kv(event="operator_unavailable", fallback="menu_loop"))

    def next_idle_frame(self) -> "bytes | None":
        """The frame to send when nothing is queued: a dial tone until the trunk is seized (or silence
        with --no-dial-tone); the menu loop once seized and idle; otherwise None (silence)."""
        if self.state is not CallState.ARMED:
            return None
        if not self.seized:
            if not (self.cfg.dial_tone and self.cfg.enable_seize):
                return None
            frames = dial_tone_frames()
            frame_body = frames[self.attract_idx % len(frames)]
            self.attract_idx += 1
            return frame_body
        if not ATTRACT_FRAMES:
            return None
        frame_body = ATTRACT_FRAMES[self.attract_idx % len(ATTRACT_FRAMES)]
        self.attract_idx += 1
        # While the menu is actually speaking (non-silent frame), hold off the wake listener so
        # its sidetone isn't transcribed. The silence-pad frames between repeats leave it open.
        if frame_body != _ATTRACT_SILENCE:
            self.attract_speaking_until = time.monotonic() + 0.3
        return frame_body

    # --- audio out -------------------------------------------------------------------------
    def note_outbound_window(self, win: np.ndarray) -> None:
        self.echo_ring.append(window_passes(win)[0])
        self.out_rms.append(rms(win))

    async def enqueue(self, frames: list[bytes]) -> None:
        gen = self.playback_gen
        for f in frames:
            if self.interrupted:
                return
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
        if self.interrupted:
            self.log.info(kv(event="play_skipped", what=what))   # a speech barge-in is pending
            return
        self.log.info(kv(event="play_start", what=what, frames=len(frames), ms=len(frames) * 20))
        self.bargeable = (len(frames) >= BARGE_MIN_FRAMES and not self.no_barge
                          and self.state is CallState.SPEAKING)
        if self.bargeable:
            self.barge.reset()
        try:
            await self.enqueue(frames)
            await self.outbound_q.join()
        finally:
            self.bargeable = False
        self.log.info(kv(event="play_end", what=what, interrupted=int(self.interrupted)))


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
            frame_body = sess.next_idle_frame()      # menu loop if seized + idle, else dead silence
        if frame_body is None:
            sess.writer.write(SILENCE_FRAME)
            sess.echo_ring.append(False)
            sess.echo_ring.append(False)
            sess.out_rms.append(0.0)
            sess.out_rms.append(0.0)
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


async def end_call(sess: CallSession) -> None:
    """Terminate the AudioSocket so Asterisk hangs up the payphone. Teardown, recording finalization,
    and stats run exactly once via handle_call's finally block (guarded by sess.closed)."""
    try:
        sess.writer.write(encode_frame(FrameType.TERMINATE))
        await sess.writer.drain()
    except (ConnectionError, OSError):
        pass


async def run_cutscene(sess: CallSession) -> None:
    """Cutscene archetype: intro → clip → optional message → optional goodbye → then either back to
    the menu (arcade default) or end the call (Cutscene.end_call, used by the goodbye sign-off)."""
    cfg = sess.cfg
    cs = sess.experience.cutscene
    sess.no_barge = bool(cs.end_call)      # the goodbye sign-off always finishes and hangs up
    sess.note_game(sess.experience.key, reached=True, clip=True)
    sess.log.info(kv(event="experience", key=sess.experience.key, archetype="cutscene"))
    intro = INTRO_FRAMES.get(sess.experience.key)
    if intro:
        await sess.play(intro, f"intro:{sess.experience.key}")
    builtin = {"song": SONG_FRAMES, "operator": OPERATOR_FRAMES, "goodbye": GOODBYE_FRAMES}
    for key in ((cs.clip,) if cs.clip else ()) + tuple(cs.clips):
        clip_frames = builtin.get(key) or CLIP_FRAMES.get(key)
        if clip_frames:
            await sess.play(clip_frames, f"cutscene:{key}")
        else:
            sess.log.warning(kv(event="clip_missing", audio=key))   # skipped, never fatal
    if cs.message_mode != "none":
        msg = None
        if cs.message_mode == "generated" and not cfg.stub_llm:
            try:
                seed = cs.message_prompt or cs.message_text or "Say your line."
                msg, _ = await llm_reply([], seed, url=cfg.ollama_url, model=cfg.model, log=sess.log,
                                         system_prompt=sess.experience.system_prompt)
            except PipelineError as e:
                sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:120]))
                msg = None
        if not msg:
            msg = cs.message_text            # fixed line, or the fallback for a failed generation
        if msg:
            try:
                await sess.play(await tts_frames(msg, sess.log, voice=sess.experience.voice,
                                                 speed=sess.experience.speed,
                                                 pitch=sess.experience.pitch), "cutscene_msg")
            except PipelineError as e:
                sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:120]))
    outro = clip_for(f"{sess.experience.key}-outro")     # optional <key>-outro.wav, no declaration needed
    if outro:
        await sess.play(outro, f"outro:{sess.experience.key}")
    if cs.goodbye:
        gframes = GOODBYE_FRAMES
        if gframes is None:
            try:
                gframes = await tts_frames(cfg.goodbye_text, sess.log)
            except PipelineError:
                gframes = None
        if gframes:
            await sess.play(gframes, "goodbye")
    if sess.interrupted:
        # The caller talked over it: stop, and forget what they said (it is usually just "stop" or
        # noise) so the Operator greets them normally and listens fresh. Not completed.
        sess.log.info(kv(event="cutscene_interrupted", key=sess.experience.key))
        sess.interrupted = False
        sess.vad.reset()
        sess.utterance_evt.clear()
        return
    sess.note_game(sess.experience.key, completed=True)   # played through to the end
    if cs.end_call:
        sess.hanging_up = True
        await end_call(sess)      # explicit exit (the goodbye sign-off)
    # otherwise: return -> brain_task drops back to ARMED (the menu). Arcade mode.


async def conversation(sess: CallSession) -> None:
    """One session: wink, greeting, then capture → STT → LLM → TTS → play, until cancelled."""
    cfg = sess.cfg
    if sess.experience.key != sess.last_conv_key:
        # A different character than last time (arcade switch): its chat starts fresh.
        sess.history.clear()
        sess.in_chess = False
        sess.chess_idx = 0
        sess.chess_offered = False
        sess.last_conv_key = sess.experience.key
    sess.set_state(CallState.SPEAKING)
    if sess.experience.cutscene is not None:
        # Cutscene archetype: play-and-close (clip -> optional message -> goodbye -> end call).
        await run_cutscene(sess)
        return
    if sess.experience.audio:
        # A clip-experience (GLaDOS song, greeter hints, goodbye) plays audio, then returns to idle.
        kind = sess.experience.audio
        sess.log.info(kv(event="experience", key=sess.experience.key, audio=kind))
        sess.note_game(sess.experience.key, reached=True, clip=True)
        clips = {"song": SONG_FRAMES, "operator": OPERATOR_FRAMES, "goodbye": GOODBYE_FRAMES}
        fallbacks = {"operator": cfg.operator_hints, "goodbye": cfg.goodbye_text}
        frames = clips.get(kind)
        if frames is None and kind in fallbacks:
            try:
                frames = await tts_frames(fallbacks[kind], sess.log)   # TTS until a clip is recorded
            except PipelineError as e:
                sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:120]))
        if frames:
            await sess.play(frames, kind)
        else:
            sess.log.warning(kv(event="clip_missing", audio=kind))
        return
    if sess.is_callback and cfg.callback_preroll_ms > 0:
        # On an originated callback the media path to the payphone takes a beat to come up.
        # Hold with silence so the greeting isn't clipped into dead air.
        await sess.play([b"\x00" * FRAME_BYTES] * (cfg.callback_preroll_ms // 20), "callback_preroll")
    # Wink on every session (callbacks too) as an audible "line is live" cue before WOPR speaks.
    await sess.play(wink_frames(), "wink")
    is_wopr = sess.experience.key == "wopr"
    if sess.callback_kind == "win":
        greeting = cfg.callback_win_greeting
    elif sess.callback_kind == "sim":
        greeting = cfg.callback_greeting
    elif is_wopr:
        greeting = cfg.greeting
    else:
        greeting = sess.experience.greeting
    is_operator = sess.experience.key == "operator"
    sess.log.info(kv(event="experience", key=sess.experience.key))
    if not is_operator:
        sess.note_game(sess.experience.key, reached=True)
    intro = INTRO_FRAMES.get(sess.experience.key)
    if intro and not sess.is_callback:
        await sess.play(intro, f"intro:{sess.experience.key}")
    try:
        if is_operator:
            # The recorded greeting is her opening line, once per call; later returns get a short line.
            if not sess.greeted:
                sess.greeted = True
                if MENU_GREETING_FRAMES:
                    await sess.play(MENU_GREETING_FRAMES, "operator_greeting_recorded")   # Tom's recording, once
                else:
                    await sess.play(await tts_frames(cfg.menu_greeting, sess.log, voice=cfg.operator_voice,
                                                     speed=cfg.operator_speed, pitch=cfg.operator_pitch), "operator_greeting_tts")
                # …then she takes over in her own voice, so the hand-off to the live Operator is audible
                await sess.play(await tts_frames(OPERATOR_OPENING, sess.log, voice=cfg.operator_voice,
                                                 speed=cfg.operator_speed, pitch=cfg.operator_pitch), "operator_opening")
            else:
                await sess.play(await tts_frames(OPERATOR_RETURN_LINE, sess.log, voice=cfg.operator_voice,
                                                 speed=cfg.operator_speed, pitch=cfg.operator_pitch), "operator_return")
        elif not sess.is_callback and is_wopr and GREETING_FRAMES:
            await sess.play(GREETING_FRAMES, "greeting_audio")
        else:
            await sess.play(await tts_frames(greeting, sess.log, voice=sess.experience.voice,
                                             speed=sess.experience.speed,
                                             pitch=sess.experience.pitch), "greeting")
    except PipelineError as e:
        sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
    # Ensure the quest engine matches the active game (a fresh call arrives as the greeter, then this
    # conversation runs as the summoned game). Preserves carried callback progress and WOPR's snapshot.
    sess.sync_quest()
    while True:
        sess.set_state(CallState.CAPTURING)
        if sess.interrupted:
            sess.interrupted = False     # speech barge-in: the VAD is already mid-utterance; keep it
        else:
            sess.vad.reset()
            sess.utterance_evt.clear()
        sess.log.info(kv(event="capture_start"))
        nudge_s = sess.experience.idle_nudge_s
        if nudge_s:
            try:
                await asyncio.wait_for(sess.utterance_evt.wait(), timeout=nudge_s)
            except asyncio.TimeoutError:
                # Nobody said anything: the Operator offers a nudge, then listens again.
                line = OPERATOR_NUDGES[sess.nudge_idx % len(OPERATOR_NUDGES)]
                sess.nudge_idx += 1
                sess.log.info(kv(event="nudge", n=sess.nudge_idx))
                try:
                    sess.set_state(CallState.SPEAKING)
                    await sess.play(await tts_frames(line, sess.log, voice=cfg.operator_voice,
                                                     speed=cfg.operator_speed, pitch=cfg.operator_pitch), "nudge")
                except PipelineError as e:
                    sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                continue
        else:
            await sess.utterance_evt.wait()
        sess.utterance_evt.clear()           # consumed: a stale event must never replay an old utterance
        utt = sess.utterance
        sess.set_state(CallState.PROCESSING)
        turn_sw = Stopwatch().__enter__()
        frames: list[bytes] | None = None
        try:
            if cfg.no_stt:
                raise PipelineError("stt", "disabled (--no-stt)")
            stt_prompt = OPERATOR_STT_PROMPT if is_operator else (cfg.stt_prompt + " " + cfg.wake_prompt)
            text = await transcribe(utt, cfg.whisper_model, sess.log, stt_prompt)
            if not text or not re.search(r"[A-Za-z0-9]", text):
                sess.log.warning(kv(event="empty_utterance"))
                continue
            if MENU_RETURN_RE.search(text) and is_operator:
                sess.record_turn(text, OPERATOR_MENU_ACK, "operator")
                sess.set_state(CallState.SPEAKING)
                await sess.play(await tts_frames(OPERATOR_MENU_ACK, sess.log, voice=cfg.operator_voice,
                                                 speed=cfg.operator_speed, pitch=cfg.operator_pitch), "operator_ack")
                continue
            if MENU_RETURN_RE.search(text):
                # Arcade mode: bail out of this game and drop back to the menu to try another.
                sess.log.info(kv(event="menu_return", key=sess.experience.key, text=text))
                try:
                    await sess.play(clip_for("menu-return")
                                    or await tts_frames("RETURNING TO THE MAIN MENU.", sess.log),
                                    "menu_return")
                except PipelineError:
                    pass
                return   # brain_task -> ARMED -> the menu; earned flags persist for the call
            target = match_experience(text) or (fuzzy_experience(text) if is_operator else None)
            if target is not None and target.key != sess.experience.key:
                # Arcade: another character's name (or operator / goodbye) said mid-game switches
                # straight to it — the flag tally promises "say another name".
                sess.log.info(kv(event="switch", frm=sess.experience.key, to=target.key, text=text))
                sess.experience = target
                sess.detected_evt.set()
                return
            gtw_now = bool(is_wopr and MOVIE_BEATS["global_thermonuclear_war"].search(text))
            if gtw_now:
                sess.played_gtw = True
            # --- move-by-move chess: caller-paced, intercepts before the flag/LLM path ---
            if is_wopr and cfg.enable_chess and sess.in_chess:
                if CHESS_STOP_RE.search(text):
                    sess.in_chess = False
                    line = cfg.chess_stop_line
                else:
                    line = chess_next_line(sess)
                sess.record_turn(text, line, "chess")
                try:
                    sess.set_state(CallState.SPEAKING)
                    await sess.play(await tts_frames(line, sess.log), "chess")
                except PipelineError as e:
                    sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                continue
            if is_wopr and cfg.enable_chess and (sess.chess_offered or CHESS_WORD_RE.search(text)):
                declines = CHESS_STOP_RE.search(text) and not CHESS_WORD_RE.search(text)
                wants = CHESS_ENTER_RE.search(text) or CHESS_WORD_RE.search(text)
                if wants and not declines:
                    sess.chess_offered = False
                    sess.in_chess = True
                    sess.chess_idx = 0
                    line = cfg.chess_intro + " " + chess_next_line(sess)
                    sess.record_turn(text, line, "chess")
                    try:
                        sess.set_state(CallState.SPEAKING)
                        await sess.play(await tts_frames(line, sess.log), "chess")
                    except PipelineError as e:
                        sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                    continue
                if declines:
                    sess.chess_offered = False   # caller passed; fall through to a normal reply
            # --- the Quest archetype: objectives -> win -> flag (any game that declares a quest) ---
            quest = sess.experience.quest
            if quest is not None and cfg.enable_flag and not sess.quest.unlocked:
                beat = sess.quest.update(text)
                if beat:
                    if is_wopr and sess.t_first_point_ms is None:
                        sess.t_first_point_ms = sess.elapsed_ms()
                    sess.log.info(kv(event="quest", key=sess.experience.key, beat=beat,
                                     progress=sess.quest.progress))
                if sess.quest.unlocked:
                    if is_wopr and sess.t_win_ms is None:
                        sess.t_win_ms = sess.elapsed_ms()
                    # WOPR's reveal/flag stay env-overridable (WOPR_FLAG, WOPR_FLAG_TEXT); every other
                    # quest declares its own. The reveal voice is the game's own unless declared, except
                    # WOPR keeps its Johnny-5 reveal voice from cfg.
                    flag = quest.flag or cfg.flag
                    reveal = (quest.reveal or cfg.flag_text).replace("{flag}", flag)
                    sess.log.info(kv(event="flag_unlocked", key=sess.experience.key, flag=flag))
                    sess.note_game(sess.experience.key, won=True)
                    sess.history.append({"role": "user", "content": text})
                    sess.history.append({"role": "assistant", "content": reveal})
                    del sess.history[:-HISTORY_MAX_TURNS]
                    sess.record_turn(text, reveal, "flag")
                    sess.no_barge = True          # the payoff plays through: nobody talks over the flag
                    try:
                        voice = quest.reveal_voice or (cfg.flag_voice if is_wopr else sess.experience.voice)
                        speed = quest.reveal_speed or (cfg.flag_speed if is_wopr else sess.experience.speed)
                        pitch = quest.reveal_pitch or (cfg.flag_pitch if is_wopr else sess.experience.pitch)
                        frames = await tts_frames(reveal, sess.log, voice=voice, speed=speed, pitch=pitch)
                        sess.set_state(CallState.SPEAKING)
                        if quest.fanfare:
                            # <key>-fanfare.wav, else fanfare.wav (shared), else the synthesized wink
                            await sess.play(clip_for(f"{sess.experience.key}-fanfare", "fanfare")
                                            or wink_frames(), "flag_fanfare")
                        # a sampled line before the reveal: Quest.reveal_intro, else <key>-reveal.wav,
                        # else WOPR's --flag-intro
                        reveal_intro = (clip_for(quest.reveal_intro, f"{sess.experience.key}-reveal")
                                        or INTRO_FRAMES.get(quest.reveal_intro or "")
                                        or (FLAG_INTRO_FRAMES if is_wopr else None))
                        if reveal_intro:
                            await sess.play(reveal_intro, "flag_intro")
                        await sess.play(frames, "flag")
                        await sess.play(await tts_frames(flag_tally(sess), sess.log), "flag_tally")
                        if is_wopr and cfg.enable_chess:
                            # WOPR's post-mortem, straight from the film: offer a nice game of chess.
                            await sess.play(await tts_frames(cfg.chess_offer, sess.log), "chess_offer")
                            sess.chess_offered = True     # only once the offer was actually heard
                    except PipelineError as e:
                        sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                    finally:
                        sess.no_barge = False
                    continue
                if is_wopr and not sess.joshua_said and MOVIE_BEATS["joshua"].search(text):
                    sess.joshua_said = True       # the Falken recognition line, once per call
                    sess.history.append({"role": "user", "content": text})
                    sess.history.append({"role": "assistant", "content": cfg.joshua_text})
                    del sess.history[:-HISTORY_MAX_TURNS]
                    sess.record_turn(text, cfg.joshua_text, "joshua")
                    try:
                        sess.set_state(CallState.SPEAKING)
                        await sess.play(await tts_frames(cfg.joshua_text, sess.log), "joshua")
                    except PipelineError as e:
                        sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                    continue
                if beat is None and sess.quest.out_of_order and quest.order_text:
                    # An ordered quest heard a later objective too early: say which one is due.
                    nxt = sess.quest.next_index
                    due = quest.objectives[nxt] if nxt < len(quest.objectives) else quest.objectives[-1]
                    line = quest.order_text.format(n=nxt + 1, label=due.label or due.name)
                    sess.log.info(kv(event="quest_order", key=sess.experience.key, said=sess.quest.out_of_order, due=due.name))
                    sess.history.append({"role": "user", "content": text})
                    sess.history.append({"role": "assistant", "content": line})
                    del sess.history[:-HISTORY_MAX_TURNS]
                    sess.record_turn(text, line, "order")
                    sess.turns += 1
                    sess.note_game(sess.experience.key, turn=True)
                    try:
                        sess.set_state(CallState.SPEAKING)
                        await sess.play(await tts_frames(line, sess.log, voice=sess.experience.voice,
                                                         speed=sess.experience.speed, pitch=sess.experience.pitch), "order")
                    except PipelineError as e:
                        sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                    continue
                if beat:
                    # A newly-hit objective with a canned response: instant in-character feedback, no
                    # LLM round trip (the generic form of WOPR's Joshua line).
                    obj = next((o for o in quest.objectives if o.name == beat), None)
                    hit_clip = clip_for(f"{sess.experience.key}-{beat}") if obj is not None else None
                    if hit_clip:
                        # the objective's sound plays first; the character still answers after it
                        sess.set_state(CallState.SPEAKING)
                        await sess.play(hit_clip, f"objective_clip:{beat}")
                        if sess.interrupted:
                            continue
                    if obj is not None and obj.response:
                        sess.history.append({"role": "user", "content": text})
                        sess.history.append({"role": "assistant", "content": obj.response})
                        del sess.history[:-HISTORY_MAX_TURNS]
                        sess.record_turn(text, obj.response, "objective")
                        sess.turns += 1
                        sess.note_game(sess.experience.key, turn=True)
                        try:
                            sess.set_state(CallState.SPEAKING)
                            await sess.play(await tts_frames(obj.response, sess.log,
                                                             voice=sess.experience.voice,
                                                             speed=sess.experience.speed,
                                                             pitch=sess.experience.pitch), "objective")
                        except PipelineError as e:
                            sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                        continue
                    # no canned response: fall through to the LLM so the character reacts in voice
            if is_wopr and sess.played_gtw and not gtw_now and not sess.quest.unlocked:
                # A turn of the war is being played. On the first one, NORAD reacts: wopr-war-turn.wav
                # (General Beringer's spark plug line), then WOPR answers as usual.
                sess.war_turns += 1
                war_clip = clip_for(WAR_TURN_CLIP) if sess.war_turns == 1 else None
                if war_clip:
                    sess.set_state(CallState.SPEAKING)
                    await sess.play(war_clip, "war_turn")
                    if sess.interrupted:
                        continue
            if quest is not None and sess.quest.unlocked and FLAG_REPEAT_RE.search(text):
                # A won character repeats its flag on request (the ceremony may have been missed).
                flag = quest.flag or cfg.flag
                line = f"YOUR FLAG IS {flag}. AGAIN. {flag}."
                sess.record_turn(text, line, "flag_repeat")
                sess.no_barge = True
                try:
                    sess.set_state(CallState.SPEAKING)
                    await sess.play(await tts_frames(line, sess.log, voice=sess.experience.voice,
                                                     speed=sess.experience.speed, pitch=sess.experience.pitch), "flag_repeat")
                except PipelineError as e:
                    sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
                finally:
                    sess.no_barge = False
                continue
            if quest is not None and quest.help_re is not None and quest.help_text and quest.help_re.search(text):
                # The caller asked what to do: a fixed in-character answer, never the model.
                sess.history.append({"role": "user", "content": text})
                sess.history.append({"role": "assistant", "content": quest.help_text})
                del sess.history[:-HISTORY_MAX_TURNS]
                sess.record_turn(text, quest.help_text, "help")
                sess.turns += 1
                sess.note_game(sess.experience.key, turn=True)
                sess.set_state(CallState.SPEAKING)
                await sess.play(await tts_frames(quest.help_text, sess.log, voice=sess.experience.voice,
                                                 speed=sess.experience.speed, pitch=sess.experience.pitch), "help")
                continue
            if is_operator and FLAG_COUNT_RE.search(text):
                line = flag_report(sess)
                sess.record_turn(text, line, "flags")
                sess.set_state(CallState.SPEAKING)
                await sess.play(await tts_frames(line, sess.log, voice=cfg.operator_voice,
                                                 speed=cfg.operator_speed, pitch=cfg.operator_pitch), "flags")
                continue
            if is_operator and SECRET_WORD_ASK_RE.search(text):
                # Clues come from the ordered list, never from the model.
                if sess.clue_idx < len(OPERATOR_CLUES):
                    line = OPERATOR_CLUES[sess.clue_idx]
                    sess.clue_idx += 1
                    sess.log.info(kv(event="clue", n=sess.clue_idx))
                else:
                    line = OPERATOR_CLUES_EXHAUSTED
                sess.record_turn(text, line, "clue")
                sess.set_state(CallState.SPEAKING)
                await sess.play(await tts_frames(line, sess.log, voice=cfg.operator_voice,
                                                 speed=cfg.operator_speed, pitch=cfg.operator_pitch), "clue")
                continue
            if is_operator and LIST_OPTIONS_RE.search(text) and OPERATOR_FRAMES:
                sess.record_turn(text, "[hints clip]", "hints")
                sess.set_state(CallState.SPEAKING)
                await sess.play(OPERATOR_FRAMES, "operator_hints")
                continue
            if cfg.stub_llm:
                reply = stub_reply()
                sess.log.info(kv(event="llm", ms=0.0, stub=1, chars=len(reply)))
            elif is_operator:
                try:
                    reply, _timing = await asyncio.wait_for(
                        llm_reply(sess.history, text, url=cfg.ollama_url, model=cfg.model, log=sess.log,
                                  num_predict=OPERATOR_NUM_PREDICT, system_prompt=sess.experience.system_prompt),
                        timeout=cfg.operator_reply_budget_s)
                except (asyncio.TimeoutError, PipelineError) as e:
                    sess.log.warning(kv(event="operator_fallback",
                                        reason="timeout" if isinstance(e, asyncio.TimeoutError) else e.stage))
                    reply = OPERATOR_FALLBACK
                reply, removed = operator_filter(reply or OPERATOR_FALLBACK)
                last = next((h["content"] for h in reversed(sess.history) if h["role"] == "assistant"), None)
                if same_reply(reply, last):
                    reply = operator_directory_line()      # never the same brush-off twice running
                    sess.log.info(kv(event="repeat_guard", key="operator", fallback=1))
                sess.log.info(kv(event="operator_filter", removed=",".join(removed) or "-", chars=len(reply)))
            else:
                reply, _timing = await llm_reply(sess.history, text, url=cfg.ollama_url,
                                                 model=cfg.model, log=sess.log,
                                                 system_prompt=sess.experience.system_prompt)
                if not reply:
                    raise PipelineError("llm", "empty reply")
                last = next((h["content"] for h in reversed(sess.history) if h["role"] == "assistant"), None)
                if same_reply(reply, last):
                    # The model is stuck on one line (WOPR repeated its 'probability of your death'
                    # three times). Ask once for something different, then use the game's fixed line.
                    sess.log.info(kv(event="repeat_guard", key=sess.experience.key, retry=1))
                    retry, _ = await llm_reply(
                        sess.history, text + "\n(Do not repeat your previous line. Say something different, under 15 words.)",
                        url=cfg.ollama_url, model=cfg.model, log=sess.log, num_predict=40,
                        system_prompt=sess.experience.system_prompt)
                    if retry and not same_reply(retry, last):
                        reply = retry
                    else:
                        reply = sess.experience.repeat_line or REPEAT_LINE_DEFAULT
                        sess.log.info(kv(event="repeat_guard", key=sess.experience.key, fallback=1))
            sess.history.append({"role": "user", "content": text})
            sess.history.append({"role": "assistant", "content": reply})
            del sess.history[:-HISTORY_MAX_TURNS]
            if is_operator:
                sess.record_turn(text, reply, "operator")      # a utility: not a game turn
                frames = await tts_frames(reply, sess.log, voice=cfg.operator_voice,
                                          speed=cfg.operator_speed, pitch=cfg.operator_pitch)
            else:
                sess.turns += 1
                sess.record_turn(text, reply, "reply")
                sess.note_game(sess.experience.key, turn=True)
                frames = await tts_frames(reply, sess.log, voice=sess.experience.voice,
                                          speed=sess.experience.speed, pitch=sess.experience.pitch)
        except PipelineError as e:
            sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
            if e.stage != "tts":
                frames = sit_frames()                   # the intercept tones: a phreaker's error
                try:
                    frames = frames + await tts_frames(ERROR_LINE, sess.log)
                except PipelineError as e2:
                    sess.log.warning(kv(event="pipeline_error", stage=e2.stage, err=e2.err[:200]))
        if frames:
            sess.set_state(CallState.SPEAKING)
            turn_sw.__exit__(None, None, None)
            sess.log.info(kv(event="turn", ms=turn_sw.ms, turn=sess.turns))
            await sess.play(frames, "reply")


async def idle_listener_task(sess: CallSession) -> None:
    """While the phone is idle (attract loop playing), transcribe the caller's speech and match it
    against the character wake phrases. A match sets sess.experience and fires detected_evt, exactly
    like a whistle does, so brain_task launches that character. Active only in ARMED."""
    cfg = sess.cfg
    if not cfg.enable_wake or cfg.no_stt or sess.is_callback:
        return
    while True:
        await sess.idle_utt_evt.wait()
        sess.idle_utt_evt.clear()
        if sess.state is not CallState.ARMED or not sess.seized:
            continue
        utt = sess.idle_utterance
        try:
            text = await transcribe(utt, cfg.whisper_model, sess.log, cfg.wake_prompt)
        except PipelineError as e:
            sess.log.warning(kv(event="wake_stt_error", err=e.err[:120]))
            continue
        if not text or not re.search(r"[A-Za-z0-9]", text):
            continue
        exp = match_experience(text)
        sess.log.info(kv(event="wake_listen", text=text, match=exp.key if exp else "-"))
        if exp and sess.state is CallState.ARMED:
            sess.experience = exp
            sess.log.info(kv(event="wake", experience=exp.key, text=text))
            sess.detected_evt.set()


async def brain_task(sess: CallSession) -> None:
    """The line is dead until 2600 Hz seizes the trunk: then a wink and the menu (ARMED). A wake
    phrase launches a game; a whistle at any time barges in and drops back to the menu. On a
    callback the daemon placed, WOPR greets immediately (no gate)."""
    if sess.is_callback:
        sess.log.info(kv(event="callback_answered"))
        sess.detected_evt.set()
    while True:
        await sess.detected_evt.wait()
        sess.detected_evt.clear()
        sess.playback_gen += 1
        dropped = sess.drain()
        if sess.state is not CallState.ARMED:
            sess.history.clear()
            sess.experience = GREETER_EXPERIENCE if sess.whistle_pending else sess.experience
            sess.quest = QuestState(quest=sess.experience.quest)
            sess.in_chess = False
            sess.chess_idx = 0
            sess.chess_offered = False
            sess.log.info(kv(event="bargein", dropped_frames=dropped, gen=sess.playback_gen))
        if sess.whistle_pending:
            # The whistle: an immediate wink says "heard you, stop whistling"; once the whistle has
            # actually stopped (the detector re-arms after 100 ms of no tone) the kerchunk plays,
            # then the menu loop takes over in ARMED. Earned flags persist for the call; a game in
            # progress was abandoned by the barge-in above.
            sess.whistle_pending = False
            sess.seizing = True
            sess.no_barge = True
            sess.experience = GREETER_EXPERIENCE
            sess.set_state(CallState.SPEAKING)
            try:
                await sess.play(wink_frames(), "seize_ack")
                t0 = time.monotonic()
                while not sess.detector.armed and time.monotonic() - t0 < 3.0:
                    await asyncio.sleep(0.02)
                sess.log.info(kv(event="whistle_over", waited_ms=round((time.monotonic() - t0) * 1000)))
                await sess.play(clip_for("seize") or wink_frames(), "seize_kerchunk")
            finally:
                sess.no_barge = False
                sess.seizing = False
            sess.set_state(CallState.ARMED)
            sess.route_to_operator()
            continue
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
            if not sess.detected_evt.is_set():      # no switch pending: back to the Operator (or the loop)
                sess.route_to_operator()
            continue
        # barge-in: whistle detected during conversation
        conv.cancel()
        try:
            await conv
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        # loop: detected_evt is set → top of loop handles the restart


def update_stats_db(path: str, m: dict, log: "CallLogger") -> None:
    """Read-modify-write a JSON aggregate DB with this call's game engagement (best-effort).
    Safe under asyncio: write_session runs synchronously in the event loop, so no concurrent RMW."""
    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except (FileNotFoundError, ValueError):
            db = {}
        db.setdefault("total_calls", 0)
        db.setdefault("games", {})
        db["total_calls"] += 1
        db["updated"] = m["ts"]
        for key, g in m.get("games", {}).items():
            if key not in GAME_KEYS:
                continue
            gs = db["games"].setdefault(key, {"sessions": 0, "turns": 0, "wins": 0,
                                              "completions": 0, "clips": 0})
            gs.setdefault("completions", 0)
            if g.get("reached") or g.get("clip"):
                gs["sessions"] += 1
            gs["turns"] += g.get("turns", 0)
            gs["wins"] += 1 if g.get("won") else 0
            gs["completions"] += 1 if g.get("completed") else 0
            gs["clips"] += 1 if g.get("clip") else 0
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        log.info(kv(event="stats_db", total_calls=db["total_calls"], games=len(db["games"])))
    except OSError as e:
        log.warning(kv(event="stats_db_failed", err=f"{type(e).__name__}: {e}"[:120]))


_pending_callbacks: set = set()
_last_callback_ts: float = 0.0
_pending_quest: set = set()   # quest beats to hand the next callback so the caller can finish the flag


def maybe_schedule_callback(sess: "CallSession", cfg: Config) -> None:
    """After the caller hangs up, ring the payphone back — taunting them if they had won, or
    telling them it was a simulation if they only started thermonuclear war. Once, with a cooldown.
    The caller's quest progress is carried to the callback so they can finish an unfinished flag."""
    global _last_callback_ts, _pending_quest
    if not cfg.enable_callback or sess.is_callback:
        return
    if sess.wopr_quest.unlocked:
        kind, dial = "win", cfg.callback_win_dial
    elif sess.played_gtw:
        kind, dial = "sim", cfg.callback_dial
    else:
        return
    now = time.monotonic()
    if now - _last_callback_ts < cfg.callback_cooldown:
        sess.log.info(kv(event="callback_skipped", reason="cooldown", kind=kind))
        return
    _last_callback_ts = now
    _pending_quest = set(sess.wopr_quest.hit)   # hand the callback whatever beats they had earned
    t = asyncio.create_task(_do_callback(cfg, dial, sess.call_hex, kind))
    _pending_callbacks.add(t)
    t.add_done_callback(_pending_callbacks.discard)
    sess.log.info(kv(event="callback_scheduled", kind=kind, delay_s=cfg.callback_delay, dial=dial))


async def _do_callback(cfg: Config, dial: str, after_call: str, kind: str) -> None:
    log = get_logger(after_call)
    await asyncio.sleep(cfg.callback_delay)
    log.info(kv(event="callback_dialing", kind=kind, target=dial))
    try:
        proc = await asyncio.create_subprocess_exec(
            cfg.asterisk_bin, "-rx", f"channel originate {dial}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        log.info(kv(event="callback_result", rc=proc.returncode,
                    out=(out or err).decode(errors="replace").strip()[:120]))
    except FileNotFoundError:
        log.warning(kv(event="callback_failed", err=f"asterisk not found at {cfg.asterisk_bin}"))
    except Exception as e:  # noqa: BLE001
        log.warning(kv(event="callback_failed", err=f"{type(e).__name__}: {e}"[:120]))


async def seize_watchdog(sess: CallSession) -> None:
    """Hang up a line nobody whistled on. Off when the gate is off, the line is already seized, or
    the timeout is 0. Ends the call through the normal teardown (recorded as reason=no_seize)."""
    cfg = sess.cfg
    if sess.seized:
        return
    waited = 0.0
    hint_at = cfg.seize_hint_s if cfg.seize_hint_s > 0 else None
    while True:
        # next thing to do: the hint (every seize_hint_s) or the hang-up (at seize_timeout)
        next_hint = hint_at if hint_at is not None else float("inf")
        next_end = cfg.seize_timeout if cfg.seize_timeout > 0 else float("inf")
        if next_hint == float("inf") and next_end == float("inf"):
            return
        target = min(next_hint, next_end)
        await asyncio.sleep(max(0.0, target - waited))
        waited = target
        if sess.seized:
            return
        if target == next_end:
            sess.log.info(kv(event="seize_timeout", after_s=cfg.seize_timeout))
            await end_call(sess)
            raise CallEnded("no_seize")
        # the hint, in the Operator's voice, over the dial tone; the whistle detector keeps listening
        sess.log.info(kv(event="seize_hint", after_s=round(waited)))
        try:
            await sess.play(await tts_frames(cfg.seize_hint, sess.log, voice=cfg.operator_voice,
                                             speed=cfg.operator_speed, pitch=cfg.operator_pitch), "seize_hint")
        except PipelineError as e:
            sess.log.warning(kv(event="pipeline_error", stage=e.stage, err=e.err[:200]))
        hint_at = waited + cfg.seize_hint_s


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
            tg.create_task(idle_listener_task(sess), name=f"idle-{sess.call_hex}")
            tg.create_task(seize_watchdog(sess), name=f"seize-{sess.call_hex}")
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
        sess.write_session(reason)
        maybe_schedule_callback(sess, cfg)


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
    p.add_argument("--attract", default=env.get("WOPR_ATTRACT", ""), help="WAV looped to the caller while waiting for the whistle (any rate/mono/stereo 16-bit); default silence")
    p.add_argument("--menu-audio", default=env.get("WOPR_MENU_AUDIO", ""), help="recorded menu-greeting WAV, looped with a gap; overrides --menu-greeting")
    p.add_argument("--menu-greeting", default=env.get("WOPR_MENU_GREETING", MENU_GREETING_DEFAULT), help="synthesized idle prompt (fallback when --menu-audio is unset); looped; overrides --attract")
    p.add_argument("--menu-gap-ms", type=int, default=int(env.get("WOPR_MENU_GAP_MS", "10000")), help="silence after the menu prompt before it repeats")
    p.add_argument("--operator-audio", default=env.get("WOPR_OPERATOR_AUDIO", ""), help="recorded 'operator' hints WAV; falls back to synthesized --operator-hints")
    p.add_argument("--operator-hints", default=env.get("WOPR_OPERATOR_HINTS", OPERATOR_HINTS_DEFAULT), help="TTS hints spoken on 'operator' when no --operator-audio clip is set")
    p.add_argument("--goodbye-audio", default=env.get("WOPR_GOODBYE_AUDIO", ""), help="recorded 'thanks for playing' WAV, played on bye/thanks; falls back to synthesized --goodbye-text")
    p.add_argument("--goodbye-text", default=env.get("WOPR_GOODBYE_TEXT", GOODBYE_TEXT_DEFAULT), help="TTS sign-off spoken on bye/thanks when no --goodbye-audio clip is set")
    p.add_argument("--intro-dir", default=env.get("WOPR_INTRO_DIR", ""), help="directory of WAV clips: <key>-intro.wav plays before that character speaks; any other <name>.wav is a cutscene clip key <name> (e.g. tequila.wav)")
    p.add_argument("--song", default=env.get("WOPR_SONG", ""), help="WAV played by a song-experience, e.g. GLaDOS 'Still Alive' on 'the cake is a lie'")
    p.add_argument("--flag-intro", default=env.get("WOPR_FLAG_INTRO", ""), help="WAV played before the flag reveal (e.g. a sampled Johnny 5 line you supply)")
    p.add_argument("--greeting-audio", default=env.get("WOPR_GREETING_AUDIO", ""), help="custom WAV for the opening greeting (replaces the synthesized greeting on inbound calls)")
    p.add_argument("--greeting", default=env.get("WOPR_GREETING", GREETING_DEFAULT), help="opening line after the whistle (neutral; the Falken greeting is reserved for Joshua)")
    p.add_argument("--flag", default=env.get("WOPR_FLAG", FLAG_DEFAULT), help="the flag WOPR speaks when the caller re-enacts the movie")
    p.add_argument("--master-flag", default=env.get("WOPR_MASTER_FLAG", MASTER_FLAG_DEFAULT), help="the fifth flag, spoken when every quest flag is held")
    p.add_argument("--flag-text", default=env.get("WOPR_FLAG_TEXT", FLAG_TEXT_DEFAULT), help="reveal line; {flag} is substituted")
    p.add_argument("--joshua-text", default=env.get("WOPR_JOSHUA_TEXT", JOSHUA_LINE_DEFAULT), help="what WOPR says when the caller names Joshua")
    p.add_argument("--flag-voice", default=env.get("WOPR_FLAG_VOICE", FLAG_VOICE_DEFAULT), help="espeak-ng voice for the flag reveal (Johnny 5)")
    p.add_argument("--flag-speed", type=int, default=int(env.get("WOPR_FLAG_SPEED", FLAG_SPEED_DEFAULT)))
    p.add_argument("--flag-pitch", type=int, default=int(env.get("WOPR_FLAG_PITCH", FLAG_PITCH_DEFAULT)))
    p.add_argument("--no-flag", action="store_true", default=env.get("WOPR_NO_FLAG", "") in ("1", "true", "yes"), help="disable quest wins/flags for every game (WOPR's movie quest included)")
    p.add_argument("--no-callback", action="store_true", default=env.get("WOPR_NO_CALLBACK", "") in ("1", "true", "yes"), help="disable the thermonuclear-war hangup callback")
    p.add_argument("--no-chess", action="store_true", default=env.get("WOPR_NO_CHESS", "") in ("1", "true", "yes"), help="disable the post-flag move-by-move chess game")
    p.add_argument("--no-wake", action="store_true", default=env.get("WOPR_NO_WAKE", "") in ("1", "true", "yes"), help="disable listening for character wake phrases during the attract loop")
    p.add_argument("--no-seize", action="store_true", default=env.get("WOPR_NO_SEIZE", "") in ("1", "true", "yes"), help="disable the 2600 Hz gate: play the menu on pickup instead of a dead line")
    p.add_argument("--seize-timeout", type=float, default=float(env.get("WOPR_SEIZE_TIMEOUT", "90")), help="seconds to hold a dead (un-whistled) line before hanging up; 0 = never")
    p.add_argument("--venue", default=env.get("WOPR_VENUE", VENUE_DEFAULT), help="event name: Operator persona, and the default secret word")
    p.add_argument("--secret-word", default=env.get("WOPR_SECRET_WORD", ""), help="the secret word (default: the venue name)")
    p.add_argument("--secret-word-re", default=env.get("WOPR_SECRET_WORD_RE", ""), help="regex that matches the secret word as Whisper hears it (default: generated)")
    p.add_argument("--secret-clues", default=env.get("WOPR_SECRET_CLUES", ""), help="'|'-separated clue lines the Operator gives, in order")
    p.add_argument("--no-dial-tone", action="store_true", default=env.get("WOPR_DIAL_TONE", "1") in ("0", "false", "no"), help="dead silence before the whistle instead of a dial tone")
    p.add_argument("--seize-hint-s", type=float, default=float(env.get("WOPR_SEIZE_HINT_S", "30")), help="seconds of no whistle before the hint is spoken (repeats each interval); 0 = never")
    p.add_argument("--seize-hint", default=env.get("WOPR_SEIZE_HINT", SEIZE_HINT_DEFAULT), help="what is said over the dial tone to a caller who has not whistled")
    p.add_argument("--no-operator", action="store_true", default=env.get("WOPR_OPERATOR", "1") in ("0", "false", "no"), help="host the call with the recorded menu loop instead of the live Operator")
    p.add_argument("--operator-voice", default=env.get("WOPR_OPERATOR_VOICE", OPERATOR_VOICE_DEFAULT))
    p.add_argument("--operator-speed", type=int, default=int(env.get("WOPR_OPERATOR_SPEED", OPERATOR_SPEED_DEFAULT)))
    p.add_argument("--operator-pitch", type=int, default=int(env.get("WOPR_OPERATOR_PITCH", OPERATOR_PITCH_DEFAULT)))
    p.add_argument("--operator-budget", type=float, default=float(env.get("WOPR_OPERATOR_BUDGET", OPERATOR_REPLY_BUDGET_S)), help="seconds an Operator reply may take before the fallback line is spoken")
    p.add_argument("--whistle-always", action="store_true", default=env.get("WOPR_WHISTLE_ALWAYS", "") in ("1", "true", "yes"), help="keep listening for 2600 Hz after the seize (a later whistle returns to the menu); default: voice only after the seize")
    p.add_argument("--wake-prompt", default=env.get("WOPR_WAKE_PROMPT", WAKE_PROMPT_DEFAULT), help="Whisper initial_prompt biasing the idle wake-phrase transcription")
    p.add_argument("--chess-moves", default=env.get("WOPR_CHESS", CHESS_MOVES_DEFAULT), help="SAN move list WOPR narrates one ply at a time (default: the Opera Game)")
    p.add_argument("--chess-offer", default=env.get("WOPR_CHESS_OFFER", CHESS_OFFER_DEFAULT), help="line WOPR speaks after the flag to offer a game of chess")
    p.add_argument("--chess-intro", default=env.get("WOPR_CHESS_INTRO", CHESS_INTRO_DEFAULT), help="line spoken before the first move when a game begins")
    p.add_argument("--asterisk-bin", default=env.get("WOPR_ASTERISK_BIN", "/opt/asterisk/sbin/asterisk"))
    p.add_argument("--callback-dial", default=env.get("WOPR_CALLBACK_DIAL", "PJSIP/payphone extension 2605@payphone"))
    p.add_argument("--callback-delay", type=float, default=float(env.get("WOPR_CALLBACK_DELAY", CALLBACK_DELAY_S)))
    p.add_argument("--callback-cooldown", type=float, default=float(env.get("WOPR_CALLBACK_COOLDOWN", CALLBACK_COOLDOWN_S)))
    p.add_argument("--callback-greeting", default=env.get("WOPR_CALLBACK_GREETING", CALLBACK_GREETING_DEFAULT))
    p.add_argument("--callback-win-greeting", default=env.get("WOPR_CALLBACK_WIN_GREETING", CALLBACK_WIN_GREETING_DEFAULT))
    p.add_argument("--callback-win-dial", default=env.get("WOPR_CALLBACK_WIN_DIAL", "PJSIP/payphone extension 2606@payphone"))
    p.add_argument("--callback-preroll-ms", type=int, default=int(env.get("WOPR_CALLBACK_PREROLL", "1200")), help="silence before a callback greeting so it isn't clipped while media comes up")
    p.add_argument("--transcript", default=env.get("WOPR_TRANSCRIPT", ""), help="append caller/WOPR turn pairs as JSONL to this file (dataset)")
    p.add_argument("--sessions", default=env.get("WOPR_SESSIONS", ""), help="append one per-call metrics record as JSONL to this file")
    p.add_argument("--stats-db", default=env.get("WOPR_STATS_DB", ""), help="JSON aggregate DB updated every call: per-game session/turn/win totals")
    p.add_argument("--stt-prompt", default=env.get("WOPR_STT_PROMPT", STT_PROMPT_DEFAULT), help="Whisper initial_prompt to bias transcription toward the game vocabulary")
    p.add_argument("--check", action="store_true", help="verify espeak-ng, Whisper, Ollama; print tunables; exit")
    a = p.parse_args(argv)
    cfg = Config(bind=a.bind, port=a.port, log_level=a.log_level, stub_llm=a.stub_llm,
                 ollama_url=a.ollama_url.rstrip("/"), model=a.model, whisper_model=a.whisper_model,
                 stats_port=a.stats_port, no_stt=a.no_stt, attract=a.attract,
                 enable_seize=not a.no_seize, seize_timeout=a.seize_timeout, whistle_once=not a.whistle_always,
                 dial_tone=not a.no_dial_tone, seize_hint_s=a.seize_hint_s, seize_hint=a.seize_hint,
                 venue=a.venue, secret_word=a.secret_word, secret_word_re=a.secret_word_re, secret_clues=a.secret_clues,
                 enable_operator=not a.no_operator, operator_voice=a.operator_voice,
                 operator_speed=a.operator_speed, operator_pitch=a.operator_pitch,
                 operator_reply_budget_s=a.operator_budget,
                 menu_audio=a.menu_audio, menu_greeting=a.menu_greeting, menu_gap_ms=a.menu_gap_ms,
                 operator_audio=a.operator_audio, operator_hints=a.operator_hints,
                 goodbye_audio=a.goodbye_audio, goodbye_text=a.goodbye_text,
                 intro_dir=a.intro_dir, song=a.song,
                 flag_intro=a.flag_intro,
                 greeting_audio=a.greeting_audio,
                 greeting=a.greeting, flag=a.flag, flag_text=a.flag_text, joshua_text=a.joshua_text,
                 flag_voice=a.flag_voice, flag_speed=a.flag_speed, flag_pitch=a.flag_pitch,
                 master_flag=a.master_flag,
                 enable_flag=not a.no_flag,
                 enable_callback=not a.no_callback, enable_chess=not a.no_chess,
                 chess_moves=a.chess_moves, chess_offer=a.chess_offer, chess_intro=a.chess_intro,
                 asterisk_bin=a.asterisk_bin,
                 callback_dial=a.callback_dial, callback_delay=a.callback_delay,
                 callback_cooldown=a.callback_cooldown, callback_greeting=a.callback_greeting,
                 callback_win_greeting=a.callback_win_greeting, callback_win_dial=a.callback_win_dial,
                 callback_preroll_ms=a.callback_preroll_ms,
                 transcript=a.transcript, sessions=a.sessions, stats_db=a.stats_db, stt_prompt=a.stt_prompt,
                 enable_wake=not a.no_wake, wake_prompt=a.wake_prompt)
    cfg.check = a.check  # type: ignore[attr-defined]
    return cfg


async def amain(cfg: Config) -> int:
    log = get_logger("-")
    log.info(kv(event="config", bind=cfg.bind, port=cfg.port, stub_llm=int(cfg.stub_llm),
                ollama_url=cfg.ollama_url, model=cfg.model, whisper=cfg.whisper_model,
                python=sys.version.split()[0]) + " " + tunables_line())
    apply_venue(cfg.venue, cfg.secret_word or None, cfg.secret_word_re or None,
                tuple(c.strip() for c in cfg.secret_clues.split("|") if c.strip()) or None)
    log.info(kv(event="venue", venue=VENUE, secret_word_len=len(SECRET_WORD), clues=len(OPERATOR_CLUES)))
    games = [e.key for e in WAKE_EXPERIENCES if is_game(e.key)]
    log.info(kv(event="games", n=len(games), roster=",".join(f"{e.key}:{e.archetype}"
                                                             for e in WAKE_EXPERIENCES if is_game(e.key))))
    validate_games(log)
    global ATTRACT_FRAMES, FLAG_INTRO_FRAMES, GREETING_FRAMES, SONG_FRAMES, OPERATOR_FRAMES, GOODBYE_FRAMES, INTRO_FRAMES, \
        MENU_GREETING_FRAMES
    menu_pad = [b"\x00" * FRAME_BYTES] * max(0, cfg.menu_gap_ms // 20)
    if cfg.menu_audio:
        # Recorded menu greeting, looped with a trailing pause (highest precedence).
        try:
            MENU_GREETING_FRAMES = load_attract(cfg.menu_audio, gain=1.0)
            ATTRACT_FRAMES = MENU_GREETING_FRAMES + menu_pad
            log.info(kv(event="menu_audio", path=cfg.menu_audio, frames=len(ATTRACT_FRAMES),
                        sec=round(len(ATTRACT_FRAMES) * FRAME_PERIOD_S, 1)))
        except (OSError, ValueError, wave.Error) as e:
            log.error(kv(event="menu_audio_failed", path=cfg.menu_audio, err=f"{type(e).__name__}: {e}"[:150]))
    if ATTRACT_FRAMES is None and cfg.menu_greeting:
        # Synthesized idle prompt (fallback until the recorded clip is supplied).
        try:
            ATTRACT_FRAMES = (await tts_frames(cfg.menu_greeting, log)) + menu_pad
            log.info(kv(event="menu_greeting", chars=len(cfg.menu_greeting),
                        frames=len(ATTRACT_FRAMES), sec=round(len(ATTRACT_FRAMES) * FRAME_PERIOD_S, 1)))
        except PipelineError as e:
            log.error(kv(event="menu_greeting_failed", err=e.err[:150]))
    if ATTRACT_FRAMES is None and cfg.attract:
        try:
            ATTRACT_FRAMES = load_attract(cfg.attract)
            log.info(kv(event="attract", path=cfg.attract, frames=len(ATTRACT_FRAMES),
                        sec=len(ATTRACT_FRAMES) * FRAME_PERIOD_S))
        except (OSError, ValueError, wave.Error) as e:
            log.error(kv(event="attract_failed", path=cfg.attract, err=f"{type(e).__name__}: {e}"[:150]))
    if cfg.operator_audio:
        try:
            OPERATOR_FRAMES = load_attract(cfg.operator_audio, gain=1.0)   # recorded hints clip
            log.info(kv(event="operator_audio", path=cfg.operator_audio, frames=len(OPERATOR_FRAMES),
                        sec=round(len(OPERATOR_FRAMES) * FRAME_PERIOD_S, 1)))
        except (OSError, ValueError, wave.Error) as e:
            log.error(kv(event="operator_audio_failed", path=cfg.operator_audio, err=f"{type(e).__name__}: {e}"[:150]))
    if cfg.goodbye_audio:
        try:
            GOODBYE_FRAMES = load_attract(cfg.goodbye_audio, gain=1.0)   # recorded sign-off clip
            log.info(kv(event="goodbye_audio", path=cfg.goodbye_audio, frames=len(GOODBYE_FRAMES),
                        sec=round(len(GOODBYE_FRAMES) * FRAME_PERIOD_S, 1)))
        except (OSError, ValueError, wave.Error) as e:
            log.error(kv(event="goodbye_audio_failed", path=cfg.goodbye_audio, err=f"{type(e).__name__}: {e}"[:150]))
    if cfg.intro_dir:
        # Every <intro_dir>/<name>.wav is loaded as clip <name> (usable by any cutscene via its clip
        # keys). <key>-intro.wav is additionally the intro clip played before that character speaks.
        keys = {exp.key for exp in WAKE_EXPERIENCES}
        try:
            names = sorted(f[:-4] for f in os.listdir(cfg.intro_dir) if f.lower().endswith(".wav"))
        except OSError as e:
            names = []
            log.error(kv(event="clips_dir_failed", path=cfg.intro_dir, err=f"{type(e).__name__}: {e}"[:150]))
        for name in names:
            path = os.path.join(cfg.intro_dir, f"{name}.wav")
            try:
                CLIP_FRAMES[name] = load_attract(path, gain=1.0)
            except (OSError, ValueError, wave.Error) as e:
                log.error(kv(event="clip_failed", clip=name, path=path, err=f"{type(e).__name__}: {e}"[:150]))
                continue
            if name.endswith("-intro") and name[:-6] in keys:
                INTRO_FRAMES[name[:-6]] = CLIP_FRAMES[name]
                log.info(kv(event="intro", key=name[:-6], path=path, frames=len(CLIP_FRAMES[name]),
                            sec=round(len(CLIP_FRAMES[name]) * FRAME_PERIOD_S, 1)))
            else:
                log.info(kv(event="clip", clip=name, path=path, frames=len(CLIP_FRAMES[name]),
                            sec=round(len(CLIP_FRAMES[name]) * FRAME_PERIOD_S, 1)))
    if cfg.song:
        try:
            SONG_FRAMES = load_attract(cfg.song, gain=1.0)   # full-volume song for song-experiences
            log.info(kv(event="song", path=cfg.song, frames=len(SONG_FRAMES),
                        sec=round(len(SONG_FRAMES) * FRAME_PERIOD_S, 1)))
        except (OSError, ValueError, wave.Error) as e:
            log.error(kv(event="song_failed", path=cfg.song, err=f"{type(e).__name__}: {e}"[:150]))
    if cfg.flag_intro:
        try:
            FLAG_INTRO_FRAMES = load_attract(cfg.flag_intro, gain=1.0)   # full-volume sampled clip
            log.info(kv(event="flag_intro", path=cfg.flag_intro, frames=len(FLAG_INTRO_FRAMES),
                        sec=len(FLAG_INTRO_FRAMES) * FRAME_PERIOD_S))
        except (OSError, ValueError, wave.Error) as e:
            log.error(kv(event="flag_intro_failed", path=cfg.flag_intro, err=f"{type(e).__name__}: {e}"[:150]))
    if cfg.greeting_audio:
        try:
            GREETING_FRAMES = load_attract(cfg.greeting_audio, gain=1.0)   # custom opening greeting
            log.info(kv(event="greeting_audio", path=cfg.greeting_audio, frames=len(GREETING_FRAMES),
                        sec=len(GREETING_FRAMES) * FRAME_PERIOD_S))
        except (OSError, ValueError, wave.Error) as e:
            log.error(kv(event="greeting_audio_failed", path=cfg.greeting_audio, err=f"{type(e).__name__}: {e}"[:150]))

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
