#!/usr/bin/env python3
"""
harness.py — phone-free test harness for wopr_daemon.py.

Acts as the Asterisk side of an AudioSocket connection: synthesizes 8 kHz clips, streams them
at real-time pace, listens to the daemon's return audio, and treats the 480 Hz wink tone as the
"whistle heard" signal. Black-box: no log parsing, no side channel.

  python harness.py synth [--out clips] [--seed N]
  python harness.py run   [--host H] [--port P] [--clips DIR] [--only a,b] [--repeat N]
                          [--json PATH] [--timeout-s S] [--conversation] [--soak N]
                          [--sidetone-db DB] [--sidetone-ms MS] [--stats-port P]
  python harness.py play  FILE.wav [--then FILE2.wav] [--host H] [--port P] [--save-return out.wav]

Exit codes: 0 all passed · 1 mismatch · 2 could not connect / setup error.
Depends only on numpy + stdlib (+ espeak-ng for the speech clips). Imports the daemon's pure
DSP helpers; the daemon must not import faster_whisper at module import time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
import wave
from dataclasses import dataclass, field, asdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wopr_daemon as wd  # noqa: E402

SR = wd.SAMPLE_RATE
FRAME = wd.FRAME_BYTES
PERIOD = wd.FRAME_PERIOD_S
LEAD_MS = 500

# Wink detector (return path): N=100 → 80 Hz bins; 480 Hz is bin 6 exactly.
WINK_N = 100
WINK_BINS = (5, 6, 7)
WINK_RATIO_MIN = 2.0
WINK_PURITY_MIN = 0.10
WINK_MIN_ENERGY = 1.0e9
WINK_HOLD = 5
WINK_REARM = 10
SILENCE_RMS = 150.0          # return-audio RMS below this is "silent"


# ─────────────────────────────────────────────────────────────────────────────────────────
# Clip synthesis
# ─────────────────────────────────────────────────────────────────────────────────────────

def _lead(ms=LEAD_MS):
    return np.zeros(int(SR * ms / 1000), dtype=np.float64)


def _fm_tone(base_hz, ms, amp, drift_hz=0.0, drift_rate=0.5, am_depth=0.0, am_rate=3.0,
             noise=0.0, rng=None):
    n = int(SR * ms / 1000)
    t = np.arange(n) / SR
    f = base_hz + drift_hz * np.sin(2 * math.pi * drift_rate * t)
    phase = 2 * math.pi * np.cumsum(f) / SR
    env = amp * (1 + am_depth * np.sin(2 * math.pi * am_rate * t))
    x = env * np.sin(phase)
    if noise and rng is not None:
        # breathy: band-limited noise around the whistle
        nz = rng.standard_normal(n)
        nz = np.convolve(nz, wd.fir_lowpass_taps(3500, SR, 32), mode="same")
        x = x + noise * nz
    return x


def _chirp(f0, f1, ms, amp):
    n = int(SR * ms / 1000)
    t = np.arange(n) / SR
    f = f0 + (f1 - f0) * t / (ms / 1000)
    return amp * np.sin(2 * math.pi * np.cumsum(f) / SR)


def _speech(text: str, rng) -> np.ndarray | None:
    """espeak-ng default voice → 8 kHz float in [-1, 1] at ≈ −12 dBFS peak. None if unavailable."""
    if shutil.which("espeak-ng") is None:
        return None
    out = subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "--stdout", "--stdin"],
                         input=text.encode(), capture_output=True)
    if out.returncode != 0:
        return None
    sr, samples = wd.parse_wav_bytes(out.stdout)
    x = wd.resample(samples, sr, SR).astype(np.float64) / 32768.0
    peak = np.max(np.abs(x)) or 1.0
    x = x / peak * 0.25
    x += 0.003 * rng.standard_normal(len(x))      # line noise ≈ −50 dBFS
    return x


SPEECH_SHORT = "Hello WOPR. I would like to play a game of chess."
SPEECH_TEXT = ("The quick brown fox jumps over the lazy dog. Professor Falken, would you like to "
               "play a game? Seventeen sixty-nine. She sells sea shells by the sea shore. "
               "Six thick thistle sticks. This is a test of the emergency broadcast system.")


def build_clips(out_dir: str, seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    os.makedirs(out_dir, exist_ok=True)
    clips: list[tuple[str, np.ndarray, bool, int | None, int | None]] = []

    lead = _lead()
    tail = _lead(500)
    clips.append(("clean_2600", np.concatenate([lead, _fm_tone(2600, 1000, 0.25), tail]), True, LEAD_MS, 500))
    clips.append(("whistle_drift",
                  np.concatenate([lead, _fm_tone(2600, 1200, 0.20, drift_hz=30, am_depth=0.3, noise=0.02, rng=rng), tail]),
                  True, LEAD_MS, 600))
    d = _fm_tone(2600, 1200, 0.20, drift_hz=30, am_depth=0.3, noise=0.02, rng=rng)
    for gap_ms in (120, 220):
        a = int(SR * gap_ms / 1000)
        d[a:a + int(SR * 0.015)] = 0.0          # two 15 ms dropouts inside the first 300 ms
    clips.append(("whistle_dropout", np.concatenate([lead, d, tail]), True, LEAD_MS, 600))
    clips.append(("short_burst", np.concatenate([lead, _fm_tone(2600, 150, 0.25), _lead(1500)]), False, LEAD_MS, None))
    sp = _speech(SPEECH_TEXT, rng)
    if sp is not None:
        clips.append(("speech_only", np.concatenate([lead, sp, tail]), False, None, None))
        short = _speech(SPEECH_SHORT, rng)
        clips.append(("speech_short", np.concatenate([lead, short, tail]), False, None, None))
        harm = sp + 0.1 * np.sin(2 * math.pi * 2500 * np.arange(len(sp)) / SR) \
                  + 0.1 * np.sin(2 * math.pi * 2700 * np.arange(len(sp)) / SR)
        clips.append(("speech_harmonics", np.concatenate([lead, harm, tail]), False, None, None))
    else:
        print("WARNING: espeak-ng not found; speech clips skipped", file=sys.stderr)
    clips.append(("sweep", np.concatenate([lead, _chirp(2000, 3200, 2000, 0.25), tail]), False, LEAD_MS, None))
    clips.append(("far_off", np.concatenate([lead, _fm_tone(2450, 1000, 0.25), _fm_tone(2750, 1000, 0.25), tail]),
                  False, LEAD_MS, None))

    manifest = []
    for name, x, expect, onset, max_ttd in clips:
        pcm = wd.f32_to_pcm16(np.clip(x, -1, 1))
        path = os.path.join(out_dir, f"{name}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(pcm.tobytes())
        manifest.append({"name": name, "file": f"{name}.wav", "expect_detect": expect,
                         "onset_ms": onset, "max_ttd_ms": max_ttd, "duration_ms": int(len(pcm) / 8)})
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        assert wf.getframerate() == SR and wf.getnchannels() == 1 and wf.getsampwidth() == 2, \
            f"{path}: need 8 kHz mono 16-bit"
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")


# ─────────────────────────────────────────────────────────────────────────────────────────
# Wink detector on the return path
# ─────────────────────────────────────────────────────────────────────────────────────────

class WinkDetector:
    def __init__(self):
        self.matrix = wd._dft_matrix(WINK_BINS, WINK_N)
        self.acc = np.zeros(0, dtype="<i2")
        self.run = 0
        self.armed = True
        self.quiet = 0
        self.events: list[float] = []      # ms timestamps (caller-supplied)

    def feed(self, pcm: np.ndarray, now_ms: float) -> bool:
        """Feed int16 samples; returns True if a wink started in this chunk."""
        self.acc = np.concatenate([self.acc, pcm])
        fired = False
        while len(self.acc) >= WINK_N:
            win, self.acc = self.acc[:WINK_N], self.acc[WINK_N:]
            e = wd.window_energies(win, self.matrix)
            e5, e6, e7 = float(e[0]), float(e[1]), float(e[2])
            total = WINK_N * float(np.sum(win.astype(np.float64) ** 2)) + 1e-9
            ok = (e6 / (e5 + e7 + 1e-9) >= WINK_RATIO_MIN and e6 >= WINK_MIN_ENERGY
                  and e6 / total >= WINK_PURITY_MIN)
            if not self.armed:
                self.quiet = 0 if ok else self.quiet + 1
                if self.quiet >= WINK_REARM:
                    self.armed = True
                    self.run = 0
                continue
            self.run = self.run + 1 if ok else 0
            if self.run >= WINK_HOLD:
                self.events.append(now_ms - (WINK_HOLD - 1) * WINK_N / 8)
                self.armed, self.run, self.quiet = False, 0, 0
                fired = True
        return fired


# ─────────────────────────────────────────────────────────────────────────────────────────
# AudioSocket client
# ─────────────────────────────────────────────────────────────────────────────────────────

class AudioSocketClient:
    """Streams outbound audio at 20 ms pace; records return audio, wink events, and timing."""

    def __init__(self, host: str, port: int, sidetone_db: float | None = None, sidetone_ms: float = 40.0):
        self.host, self.port = host, port
        self.sidetone_gain = 10 ** (sidetone_db / 20) if sidetone_db is not None else 0.0
        self.sidetone_delay = int(SR * sidetone_ms / 1000)
        self.out_buf = np.zeros(0, dtype=np.float64)     # pending outbound audio (float)
        self.out_lock = asyncio.Lock()
        self.ret_pcm = bytearray()
        self.ret_times: list[float] = []                 # arrival monotonic per return frame
        self.ret_nonsilent: list[float] = []             # ms timestamps of non-silent return frames
        self.wink = WinkDetector()
        self.t0 = 0.0
        self.frames_sent = 0
        self.reader = self.writer = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.eof_ms: float | None = None
        self._recent_ret = np.zeros(0, dtype=np.float64)  # for sidetone mixing
        self.errors: list[str] = []

    # --- lifecycle -----------------------------------------------------------------------
    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self.writer.write(wd.encode_frame(wd.FrameType.UUID, uuid.uuid4().bytes))
        await self.writer.drain()
        self.t0 = time.monotonic()
        self._tasks = [asyncio.create_task(self._sender()), asyncio.create_task(self._receiver())]

    def now_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000.0

    async def queue_audio(self, pcm: np.ndarray, gain: float = 1.0) -> float:
        """Append audio (int16 or float) to the outbound program. Returns the ms at which it will start."""
        x = pcm.astype(np.float64) / 32768.0 if np.issubdtype(pcm.dtype, np.integer) else pcm.astype(np.float64)
        async with self.out_lock:
            start_ms = self.frames_sent * 20 + len(self.out_buf) / 8
            self.out_buf = np.concatenate([self.out_buf, x * gain])
        return start_ms

    async def wait_until_sent(self):
        while len(self.out_buf) > 0 and not self._stop.is_set():
            await asyncio.sleep(0.02)

    async def terminate(self, eof_timeout_s: float = 2.0) -> float | None:
        """Send TERMINATE, wait for EOF, return ms from TERMINATE to EOF (None on timeout)."""
        self._stop.set()
        others = [t for t in self._tasks if t is not asyncio.current_task()]
        for t in others:
            t.cancel()
        await asyncio.gather(*others, return_exceptions=True)
        try:
            self.writer.write(wd.encode_frame(wd.FrameType.TERMINATE))
            await self.writer.drain()
        except (ConnectionError, OSError):
            return 0.0
        t_term = time.monotonic()
        try:
            await asyncio.wait_for(self._read_to_eof(), timeout=eof_timeout_s)
            self.eof_ms = (time.monotonic() - t_term) * 1000.0
        except asyncio.TimeoutError:
            self.eof_ms = None
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            self.eof_ms = (time.monotonic() - t_term) * 1000.0
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        return self.eof_ms

    async def _read_to_eof(self):
        while True:
            data = await self.reader.read(4096)
            if not data:
                return

    async def close_abruptly(self):
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.writer.close()

    # --- tasks -----------------------------------------------------------------------------
    async def _sender(self):
        deadline = time.monotonic()
        while not self._stop.is_set():
            deadline += PERIOD
            d = deadline - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)
            async with self.out_lock:
                if len(self.out_buf) >= wd.FRAME_SAMPLES:
                    chunk, self.out_buf = self.out_buf[:wd.FRAME_SAMPLES], self.out_buf[wd.FRAME_SAMPLES:]
                elif len(self.out_buf) > 0:
                    chunk = np.concatenate([self.out_buf, np.zeros(wd.FRAME_SAMPLES - len(self.out_buf))])
                    self.out_buf = np.zeros(0)
                else:
                    chunk = np.zeros(wd.FRAME_SAMPLES)
            if self.sidetone_gain > 0 and len(self._recent_ret) >= self.sidetone_delay + wd.FRAME_SAMPLES:
                seg = self._recent_ret[-(self.sidetone_delay + wd.FRAME_SAMPLES):len(self._recent_ret) - self.sidetone_delay]
                chunk = chunk + self.sidetone_gain * seg
            frame = wd.f32_to_pcm16(np.clip(chunk, -1, 1)).tobytes()
            try:
                self.writer.write(wd.encode_frame(wd.FrameType.AUDIO, frame))
                await self.writer.drain()
            except (ConnectionError, OSError):
                self.errors.append("send failed")
                self._stop.set()
                return
            self.frames_sent += 1

    async def _receiver(self):
        while not self._stop.is_set():
            try:
                ftype, payload = await wd.read_frame(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.eof_ms = self.now_ms()
                self._stop.set()
                return
            if ftype != wd.FrameType.AUDIO:
                continue
            now = time.monotonic()
            self.ret_times.append(now)
            pcm = np.frombuffer(payload, dtype="<i2")
            self.ret_pcm += payload
            ms = (now - self.t0) * 1000.0
            if wd.rms(pcm) > SILENCE_RMS:
                self.ret_nonsilent.append(ms)
            self.wink.feed(pcm, ms)
            f = pcm.astype(np.float64) / 32768.0
            self._recent_ret = np.concatenate([self._recent_ret, f])[-SR:]   # keep last 1 s

    # --- queries ---------------------------------------------------------------------------
    async def wait_for_wink(self, count: int, timeout_ms: float) -> float | None:
        t_end = self.now_ms() + timeout_ms
        while self.now_ms() < t_end and not self._stop.is_set():
            if len(self.wink.events) >= count:
                return self.wink.events[count - 1]
            await asyncio.sleep(0.01)
        return self.wink.events[count - 1] if len(self.wink.events) >= count else None

    async def wait_nonsilent(self, min_ms: float, timeout_ms: float, after_ms: float = 0.0) -> float | None:
        """Wait until ≥ min_ms of non-silent return audio has arrived after `after_ms`. Returns first ms.
        The timeout counts from `after_ms` (which may be in the future) or from now, whichever is later."""
        t_end = max(self.now_ms(), after_ms) + timeout_ms
        while self.now_ms() < t_end and not self._stop.is_set():
            pts = [m for m in self.ret_nonsilent if m >= after_ms]
            if len(pts) * 20 >= min_ms:
                return pts[0]
            await asyncio.sleep(0.02)
        return None

    async def wait_silent(self, quiet_ms: float, timeout_ms: float) -> float | None:
        """Wait until the return audio has been silent for quiet_ms. Returns the ms the silence began."""
        t_end = self.now_ms() + timeout_ms
        while self.now_ms() < t_end and not self._stop.is_set():
            last = self.ret_nonsilent[-1] if self.ret_nonsilent else 0.0
            if self.now_ms() - last >= quiet_ms:
                return last
            await asyncio.sleep(0.02)
        return None

    def pacing_intervals_ms(self) -> list[float]:
        ts = self.ret_times[25:]
        return [(b - a) * 1000.0 for a, b in zip(ts, ts[1:])]

    def save_return(self, path: str):
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(bytes(self.ret_pcm))


# ─────────────────────────────────────────────────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    name: str
    kind: str                       # "clip" | "scenario"
    expect: str = "-"
    detected: bool | None = None
    ttd_ms: float | None = None
    passed: bool = False
    notes: str = ""
    extra: dict = field(default_factory=dict)


def fmt_table(results: list[Result]) -> str:
    rows = [("clip/scenario", "expect", "detected", "ttd_ms", "result", "notes")]
    for r in results:
        rows.append((r.name, r.expect, "-" if r.detected is None else ("yes" if r.detected else "no"),
                     "-" if r.ttd_ms is None else f"{r.ttd_ms:.0f}", "PASS" if r.passed else "FAIL", r.notes))
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) for row in rows]
    lines.insert(1, "  ".join("-" * wdt for wdt in widths))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Runs
# ─────────────────────────────────────────────────────────────────────────────────────────

async def run_clip(args, entry: dict, pcm: np.ndarray, intervals: list[float]) -> Result:
    expect = entry["expect_detect"]
    r = Result(entry["name"], "clip", "detect" if expect else "none")
    c = AudioSocketClient(args.host, args.port, args.sidetone_db if args.sidetone_clips else None, args.sidetone_ms)
    try:
        await c.connect()
    except OSError as e:
        r.notes = f"connect failed: {e}"
        return r
    try:
        start_ms = await c.queue_audio(pcm)
        dur_ms = len(pcm) / 8
        if expect:
            wink_ms = await c.wait_for_wink(1, dur_ms + args.timeout_s * 1000)
            r.detected = wink_ms is not None
            if wink_ms is not None:
                r.ttd_ms = wink_ms - (start_ms + (entry["onset_ms"] or 0))
                lim = entry.get("max_ttd_ms") or 500
                r.passed = r.ttd_ms <= lim
                r.notes = f"wink at {wink_ms:.0f} ms" + ("" if r.passed else f" (> {lim} ms)")
            else:
                r.notes = f"no wink within {dur_ms + args.timeout_s * 1000:.0f} ms"
        else:
            await asyncio.sleep((dur_ms + 1500) / 1000)   # clip + hold + margin
            r.detected = len(c.wink.events) > 0
            r.passed = not r.detected
            r.notes = "no wink" if r.passed else f"FALSE POSITIVE wink at {c.wink.events[0]:.0f} ms"
    finally:
        await c.terminate()
        intervals.extend(c.pacing_intervals_ms())
    return r


async def scenario_hangup(args) -> Result:
    r = Result("hangup", "scenario")
    c = AudioSocketClient(args.host, args.port, args.sidetone_db, args.sidetone_ms)
    await c.connect()
    await c.queue_audio(load_wav(os.path.join(args.clips, "clean_2600.wav")))
    wink = await c.wait_for_wink(1, 4000)
    eof = await c.terminate()
    r.detected = wink is not None
    if eof is None:
        r.notes = "no EOF within 2 s after TERMINATE"
    else:
        r.ttd_ms = eof
        r.passed = wink is not None and eof <= 1000
        r.notes = f"{eof:.0f} ms to EOF" + ("" if wink else "; no wink")
    return r


async def scenario_bargein(args, name="bargein", second_clip="clean_2600", second_gain=1.0) -> Result:
    r = Result(name, "scenario")
    c = AudioSocketClient(args.host, args.port, args.sidetone_db, args.sidetone_ms)
    await c.connect()
    try:
        await c.queue_audio(load_wav(os.path.join(args.clips, "clean_2600.wav")))
        wink1 = await c.wait_for_wink(1, 4000)
        if wink1 is None:
            r.notes = "first whistle not detected"
            return r
        # greeting must be playing: ≥ 200 ms of non-silent audio after the wink ends
        greet = await c.wait_nonsilent(200, 6000, after_ms=wink1 + wd.WINK_MS + 50)
        if greet is None:
            r.notes = "no greeting audio after wink"
            return r
        await asyncio.sleep(0.15)
        second = load_wav(os.path.join(args.clips, f"{second_clip}.wav"))
        # strip the clip's 500 ms lead so onset == queue time
        second = second[int(SR * LEAD_MS / 1000):]
        t_on = await c.queue_audio(second, gain=second_gain)
        still_playing = await c.wait_nonsilent(20, 400, after_ms=t_on)
        wink2 = await c.wait_for_wink(2, 4000)
        r.detected = wink2 is not None
        if wink2 is None:
            r.notes = "second whistle not detected during playback"
            return r
        latency = wink2 - (t_on + wd.HOLD_WINDOWS * 10)
        r.ttd_ms = latency
        r.passed = still_playing is not None and latency <= 150
        r.notes = (f"cut {latency:.0f} ms after hold" + ("" if still_playing else "; playback had already ended")
                   + ("" if latency <= 150 else " (> 150 ms)"))
        r.extra = {"wink1_ms": wink1, "greeting_ms": greet, "second_onset_ms": t_on, "wink2_ms": wink2}
    finally:
        await c.terminate()
    return r


async def scenario_selftrigger(args) -> Result:
    r = Result("selftrigger", "scenario")
    db = args.sidetone_db if args.sidetone_db is not None else -12.0
    c = AudioSocketClient(args.host, args.port, db, args.sidetone_ms)
    await c.connect()
    try:
        await c.queue_audio(load_wav(os.path.join(args.clips, "clean_2600.wav")))
        wink1 = await c.wait_for_wink(1, 4000)
        if wink1 is None:
            r.notes = "first whistle not detected"
            return r
        await asyncio.sleep(args.selftrigger_s)
        n = len(c.wink.events)
        r.detected = n > 1
        r.passed = n == 1
        r.notes = f"{n} wink(s) in {args.selftrigger_s:.0f} s with sidetone {db:.0f} dB @ {args.sidetone_ms:.0f} ms"
    finally:
        await c.terminate()
    return r


async def scenario_conversation(args) -> Result:
    r = Result("conversation", "scenario")
    c = AudioSocketClient(args.host, args.port, args.sidetone_db, args.sidetone_ms)
    await c.connect()
    try:
        await c.queue_audio(load_wav(os.path.join(args.clips, "clean_2600.wav")))
        wink1 = await c.wait_for_wink(1, 4000)
        if wink1 is None:
            r.notes = "whistle not detected"
            return r
        if await c.wait_silent(600, 15000) is None:
            r.notes = "greeting never ended"
            return r
        speech = load_wav(os.path.join(args.clips, "speech_short.wav"))
        t_on = await c.queue_audio(speech)
        t_end = t_on + len(speech) / 8
        reply = await c.wait_nonsilent(100, (len(speech) / 8) + args.reply_timeout_s * 1000, after_ms=t_end)
        r.detected = reply is not None
        if reply is None:
            r.notes = f"no reply audio within {args.reply_timeout_s:.0f} s after speech"
            return r
        r.ttd_ms = reply - t_end
        r.passed = True
        r.notes = f"reply {r.ttd_ms:.0f} ms after speech ended"
    finally:
        await c.terminate()
    return r


async def fetch_stats(port: int) -> dict | None:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        data = await asyncio.wait_for(reader.readline(), timeout=2)
        writer.close()
        return json.loads(data)
    except Exception:  # noqa: BLE001
        return None


async def scenario_soak(args) -> Result:
    r = Result("soak", "scenario")
    n = args.soak
    before = await fetch_stats(args.stats_port) if args.stats_port else None
    tone = load_wav(os.path.join(args.clips, "clean_2600.wav"))
    worst_eof, misses, eof_fail = 0.0, 0, 0
    for i in range(n):
        c = AudioSocketClient(args.host, args.port)
        await c.connect()
        await c.queue_audio(tone)
        w = await c.wait_for_wink(1, 3000)
        eof = await c.terminate()
        misses += w is None
        if eof is None or eof > 1000:
            eof_fail += 1
        worst_eof = max(worst_eof, eof or 9999)
    await asyncio.sleep(1.0)
    after = await fetch_stats(args.stats_port) if args.stats_port else None
    r.ttd_ms = worst_eof
    notes = f"{n} cycles, {misses} missed winks, {eof_fail} slow EOFs, worst EOF {worst_eof:.0f} ms"
    ok = misses == 0 and eof_fail == 0
    if before and after:
        dt = after["tasks"] - before["tasks"]
        drss = after["rss_mb"] - before["rss_mb"]
        notes += f"; tasks {before['tasks']}→{after['tasks']}, rss +{drss:.1f} MB"
        ok = ok and dt <= 0 and drss < 20
        r.extra = {"before": before, "after": after}
    r.passed = ok
    r.notes = notes
    return r


def pacing_result(intervals: list[float]) -> Result:
    r = Result("pacing", "scenario")
    if len(intervals) < 50:
        r.notes = "not enough return frames"
        return r
    arr = np.array(intervals)
    p95, mx = float(np.percentile(arr, 95)), float(arr.max())
    r.passed = p95 <= 30 and mx <= 100
    r.ttd_ms = p95
    r.notes = f"p95 {p95:.1f} ms, max {mx:.1f} ms over {len(arr)} frames"
    return r


async def cmd_run(args) -> int:
    manifest_path = os.path.join(args.clips, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"no manifest at {manifest_path}; run `harness.py synth` first", file=sys.stderr)
        return 2
    with open(manifest_path) as f:
        manifest = json.load(f)
    only = set(args.only.split(",")) if args.only else None
    print(f"harness → {args.host}:{args.port}   tunables: {wd.tunables_line()}")
    # connectivity probe
    try:
        rd, wr = await asyncio.open_connection(args.host, args.port)
        wr.close()
    except OSError as e:
        print(f"cannot connect to {args.host}:{args.port}: {e}", file=sys.stderr)
        return 2

    results: list[Result] = []
    intervals: list[float] = []
    for entry in manifest:
        if only and entry["name"] not in only:
            continue
        pcm = load_wav(os.path.join(args.clips, entry["file"]))
        for i in range(args.repeat):
            res = await run_clip(args, entry, pcm, intervals)
            if args.repeat > 1:
                res.name = f"{entry['name']}#{i + 1}"
            results.append(res)
            print(f"  {res.name:<22} {'PASS' if res.passed else 'FAIL'}  {res.notes}", flush=True)

    scenarios = {
        "hangup": scenario_hangup,
        "bargein": scenario_bargein,
        "selftrigger": scenario_selftrigger,
        "whistle_over_tts": lambda a: scenario_bargein(a, "whistle_over_tts", "whistle_drift", 0.5),
    }
    if args.conversation:
        scenarios["conversation"] = scenario_conversation
    if args.soak:
        scenarios["soak"] = scenario_soak
    for name, fn in scenarios.items():
        if only and name not in only:
            continue
        try:
            res = await fn(args)
        except Exception as e:  # noqa: BLE001
            res = Result(name, "scenario", notes=f"crashed: {type(e).__name__}: {e}")
        results.append(res)
        print(f"  {res.name:<22} {'PASS' if res.passed else 'FAIL'}  {res.notes}", flush=True)
    if not only or "pacing" in only:
        results.append(pacing_result(intervals))

    print()
    print(fmt_table(results))
    clips = [r for r in results if r.kind == "clip"]
    det_exp = [r for r in clips if r.expect == "detect"]
    fp = [r for r in clips if r.expect == "none" and r.detected]
    n_pass = sum(r.passed for r in results)
    print(f"\nSummary: {n_pass} passed, {len(results) - n_pass} failed  "
          f"(detect rate {sum(bool(r.detected) for r in det_exp)}/{len(det_exp)}, "
          f"false positives {len(fp)}/{len(clips) - len(det_exp)})")
    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=1)
    return 0 if n_pass == len(results) else 1


async def cmd_play(args) -> int:
    c = AudioSocketClient(args.host, args.port, args.sidetone_db, args.sidetone_ms)
    try:
        await c.connect()
    except OSError as e:
        print(f"cannot connect: {e}", file=sys.stderr)
        return 2
    pcm = load_wav(args.file)
    t0 = await c.queue_audio(pcm)
    end_ms = t0 + len(pcm) / 8
    if args.then:
        await c.wait_until_sent()
        if args.then_after_silence:
            await c.wait_silent(600, 15000)
        pcm2 = load_wav(args.then)
        t1 = await c.queue_audio(pcm2)
        end_ms = t1 + len(pcm2) / 8
        print(f"second clip queued at {t1:.0f} ms")
    reply = await c.wait_nonsilent(100, (end_ms - c.now_ms()) + args.timeout_s * 1000, after_ms=end_ms)
    await asyncio.sleep(min(args.timeout_s, 3.0))
    winks = list(c.wink.events)
    print(f"winks: {[round(x) for x in winks]} ms")
    print("reply audio: " + (f"{reply:.0f} ms ({reply - end_ms:.0f} ms after clip end)" if reply else "none"))
    if args.save_return:
        c.save_return(args.save_return)
        print(f"return audio saved to {args.save_return} ({len(c.ret_pcm) / 16000:.1f} s)")
    await c.terminate()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("synth")
    ps.add_argument("--out", default="clips")
    ps.add_argument("--seed", type=int, default=2600)

    def common(sp):
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=9092)
        sp.add_argument("--sidetone-db", type=float, default=-12.0, help="sidetone level for scenarios (None to disable)")
        sp.add_argument("--sidetone-ms", type=float, default=40.0)
        sp.add_argument("--timeout-s", type=float, default=10.0)

    pr = sub.add_parser("run")
    common(pr)
    pr.add_argument("--clips", default="clips")
    pr.add_argument("--only", default="")
    pr.add_argument("--repeat", type=int, default=1)
    pr.add_argument("--json", default="")
    pr.add_argument("--sidetone-clips", action="store_true", help="also apply sidetone during clip runs")
    pr.add_argument("--conversation", action="store_true", help="add the conversation scenario (needs STT)")
    pr.add_argument("--reply-timeout-s", type=float, default=15.0)
    pr.add_argument("--soak", type=int, default=0, help="add a soak scenario of N connect/hangup cycles")
    pr.add_argument("--stats-port", type=int, default=0)
    pr.add_argument("--selftrigger-s", type=float, default=20.0)

    pa = sub.add_parser("attract", help="render spoken text (or resample a WAV) to an 8 kHz attract-loop file")
    pa.add_argument("--text", default=("Welcome to the Aperture Science computer aided enrichment line. "
                                       "To begin testing, whistle at two thousand six hundred hertz. "
                                       "Thank you for helping us help you help us all."))
    pa.add_argument("--from-wav", default="", help="convert this WAV instead of speaking --text")
    pa.add_argument("--voice", default="en-us+f3")
    pa.add_argument("--speed", type=int, default=150)
    pa.add_argument("--out", default="clips/attract.wav")

    pp = sub.add_parser("play")
    common(pp)
    pp.add_argument("file")
    pp.add_argument("--then", default="")
    pp.add_argument("--then-after-silence", action="store_true", help="wait for the greeting to end before the second clip")
    pp.add_argument("--save-return", default="")

    args = p.parse_args(argv)
    if args.cmd == "synth":
        m = build_clips(args.out, args.seed)
        for e in m:
            print(f"  {e['name']:<18} {e['duration_ms']:>6} ms  expect={'detect' if e['expect_detect'] else 'none'}")
        print(f"{len(m)} clips → {args.out}/manifest.json")
        return 0
    if args.cmd == "run":
        return asyncio.run(cmd_run(args))
    if args.cmd == "attract":
        if args.from_wav:
            sr, samples = wd.load_wav_file(args.from_wav)
        else:
            out = subprocess.run(["espeak-ng", "-v", args.voice, "-s", str(args.speed), "--stdout", "--stdin"],
                                 input=args.text.encode(), capture_output=True)
            if out.returncode != 0:
                print("espeak-ng failed", file=sys.stderr); return 2
            sr, samples = wd.parse_wav_bytes(out.stdout)
        pcm8k = wd.resample(samples, sr, SR)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with wave.open(args.out, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
            wf.writeframes(pcm8k.astype("<i2").tobytes())
        print(f"wrote {args.out}  ({len(pcm8k)/SR:.1f}s, 8 kHz mono 16-bit)")
        return 0
    if args.cmd == "play":
        return asyncio.run(cmd_play(args))
    return 2


if __name__ == "__main__":
    sys.exit(main())
