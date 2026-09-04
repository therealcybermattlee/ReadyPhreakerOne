# Data Model: 2600 Hz Whistle → WOPR AudioSocket Daemon

**Date**: 2026-09-04 (rev 2) | **Plan**: [plan.md](plan.md)

No persistent storage. All entities are in-memory, scoped to one call, and destroyed on hangup.

## Entities

### CallSession

One per accepted TCP connection.

| Field | Type | Notes |
|---|---|---|
| `call_id` | `bytes[16]` / hex str | From the first (UUID) frame. Logged as first 8 hex chars. |
| `state` | `CallState` | See state machine below. |
| `reader`, `writer` | asyncio streams | Socket handles. Only the writer task writes after handshake. |
| `inbound_acc` | `bytearray` | Accumulates audio payload; consumed in 160-byte windows. |
| `outbound_q` | `asyncio.Queue[bytes]` | 320-byte frames awaiting the paced writer. Drained on barge-in. |
| `detector` | `ToneDetector` | Inbound 2600 Hz run state. |
| `echo_ring` | `deque[bool]` (maxlen `ECHO_GUARD_WINDOWS`) | Outbound window verdicts for the self-trigger guard. |
| `vad` | `VadState` | Noise floor, speech/silence counters. |
| `utterance` | `bytearray` | Caller audio being captured (int16 LE, 8 kHz). |
| `history` | `list[dict]` | LLM turns `{"role": "user"\|"assistant", "content": str}` in Ollama `/api/chat` shape; capped at 20; cleared on barge-in. |
| `detected_evt` | `asyncio.Event` | Set by reader on detection; consumed by brain. |
| `utterance_evt` | `asyncio.Event` | Set by reader on endpoint; consumed by brain. |
| `t_start` | monotonic float | For per-call timing. |
| `stage_timers` | `dict[str, float]` | Latest elapsed ms per pipeline stage (for logs). |

**Validation**: first frame MUST be type `0x01` with length 16, else the session is rejected.
`outbound_q` is bounded (e.g. 1500 frames = 30 s) so a runaway TTS cannot exhaust memory;
producers await on put.

### CallState (enum) and transitions

```
        ┌──────────────────────────────────────────────────────────────┐
        │                        whistle (any state)                   │
        ▼                                                              │
   ┌─────────┐  whistle   ┌──────────┐  queue drained  ┌───────────┐   │
   │  ARMED  │ ─────────► │ SPEAKING │ ──────────────► │ CAPTURING │ ──┘
   └─────────┘            │(wink+     │                 └─────┬─────┘
        ▲                 │ greeting/ │                       │ utterance endpointed
        │ (never)         │ reply)    │ ◄──────────────┐      ▼
        │                 └──────────┘   TTS enqueued   │ ┌────────────┐
        │                                               └─│ PROCESSING │ (STT→LLM→TTS)
        │                                                 └────────────┘
        │                        terminate / EOF / error (any state)
        └────────────────────────────────►  CLOSED
```

| From | Event | To | Side effects |
|---|---|---|---|
| ARMED | detection | SPEAKING | log `event=detect`; enqueue wink + greeting; `history=[]` |
| SPEAKING | outbound_q empty and last frame sent | CAPTURING | reset VAD; log `event=capture_start` |
| CAPTURING | utterance endpointed | PROCESSING | log `stage=capture ms=` |
| PROCESSING | STT empty | CAPTURING | log `event=empty_utterance` |
| PROCESSING | reply synthesized | SPEAKING | append user+assistant turns; enqueue frames |
| PROCESSING | STT/LLM/TTS error | SPEAKING (canned line) or CAPTURING | log WARNING/ERROR; `event=pipeline_error stage=` |
| SPEAKING / CAPTURING / PROCESSING | detection (barge-in) | SPEAKING | drain `outbound_q`; cancel in-flight PROCESSING; `history=[]`; log `event=bargein`; enqueue wink + greeting |
| any | terminate / EOF / socket error | CLOSED | cancel TaskGroup; close writer; log `event=call_end dur_ms=` |

Invariants:
- The detector runs in every state except CLOSED.
- Only the writer task writes to the socket after the UUID frame is read.
- `history` is empty whenever the state is ARMED or immediately after a barge-in.

### ToneDetector

Pure state, updated once per 80-sample inbound window.

| Field | Type | Notes |
|---|---|---|
| `coeffs` | 3 floats | Goertzel coefficients for k = 25, 26, 27 (precomputed). |
| `run` | int | Consecutive-ish passing windows. |
| `misses` | int | Misses spent inside the current run. |
| `last_ratio` | float | For DEBUG logging. |

Update rule per window (`masked` comes from the echo ring):

```
if masked:            (no change)
elif pass:            run += 1; if run >= HOLD_WINDOWS: fire(); run = 0; misses = 0
elif run > 0 and misses < MISS_BUDGET: misses += 1
else:                 run = 0; misses = 0
```

`pass` ≡ `E26 / (E25 + E27 + EPS) >= RATIO_THRESHOLD and E26 >= MIN_BIN_ENERGY`.

Tunables (module-level constants, top of file): `GOERTZEL_N=80`, `BINS_HZ=(2500, 2600, 2700)`,
`RATIO_THRESHOLD=0.35`, `MIN_BIN_ENERGY=<measured>`, `HOLD_WINDOWS=30`, `MISS_BUDGET=2`,
`ECHO_GUARD_WINDOWS=20`, `REARM_WINDOWS=50` (windows after a fire during which a new run cannot
start, so one long whistle fires once).

### VadState

| Field | Type | Notes |
|---|---|---|
| `noise_floor` | float | EMA of RMS during non-speech; initialised from first 200 ms. |
| `in_speech` | bool | |
| `speech_windows` | int | Consecutive above-threshold windows (start hysteresis). |
| `silence_windows` | int | Consecutive below-threshold windows (endpoint). |
| `utterance_windows` | int | Length guard. |

Tunables: `VAD_START_RATIO=3.0`, `VAD_ABS_MIN=<measured>`, `VAD_START_WINDOWS=3`,
`ENDPOINT_MS=800`, `MIN_UTTERANCE_MS=300`, `MAX_UTTERANCE_MS=15000`.

### Utterance

Transient: `bytes` of int16 LE 8 kHz audio plus `duration_ms` and `t_end` (monotonic). Converted
to float32 16 kHz for STT and then discarded.

### ConversationTurn

Two entries appended to `history` per successful exchange: `{"role":"user","content":<stt
text>}` and `{"role":"assistant","content":<post-processed WOPR text>}`. The synthesized audio is
not retained after playback.

### WoprPersona (constant)

System prompt text plus a list of canned lines: `GREETING` ("GREETINGS PROFESSOR FALKEN."),
`ERROR_LINE`, and `STUB_LINES` used by `--stub-llm`. Post-processing rules: uppercase, strip
markdown, collapse whitespace, truncate to `MAX_REPLY_CHARS=280` at a sentence boundary.

### HostServices (deployment topology, not runtime state)

Recorded here because provisioning tasks and `--verify` reason about it.

| Service | Label | Runs as | Listens | Depends on |
|---|---|---|---|---|
| Ollama | `com.crunchwhistle.ollama` | `mattlee` | `127.0.0.1:11434` | model files under `~mattlee/.ollama/models` (`qwen3:8b` present) |
| Asterisk | `com.crunchwhistle.asterisk` | `mattlee` | `0.0.0.0:5060/udp`, RTP `10000–10100/udp` | configs under `/opt/asterisk/etc/asterisk` |
| WOPR daemon | `com.crunchwhistle.wopr` | `mattlee` | `127.0.0.1:9092/tcp` | Ollama (soft, retried), espeak-ng, Whisper cache |
| HT802 (external) | — | — | SIP client on LAN | registers to Asterisk as endpoint `payphone` |

Call path: payphone → HT802 (FXS→SIP, PCMU) → Asterisk `[payphone]` ext 2600 → `AudioSocket()` →
daemon → Ollama / Whisper / espeak-ng → back the same way. No hop leaves the mini except the
LAN leg to the ATA.

### Harness: TestClip

| Field | Type | Notes |
|---|---|---|
| `name` | str | e.g. `whistle_drift` |
| `path` | Path | `clips/<name>.wav` (8 kHz mono int16) |
| `expect_detect` | bool | |
| `onset_ms` | int \| None | Tone onset for time-to-detect; None for no-detect clips |
| `max_ttd_ms` | int \| None | Pass bound when `expect_detect` |

### Harness: ClipResult

| Field | Type | Notes |
|---|---|---|
| `name` | str | |
| `detected` | bool | Wink seen in return audio |
| `ttd_ms` | int \| None | wink frame − onset |
| `passed` | bool | matches expectation and bound |
| `notes` | str | e.g. "wink at 1.31 s", "no wink in 6.0 s" |

Scenario results (`bargein`, `hangup`) reuse the shape with `ttd_ms` reinterpreted as
"ms to silence" / "ms to EOF".
