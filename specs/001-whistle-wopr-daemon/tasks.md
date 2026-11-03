---

description: "Task list for 2600 Hz Whistle → WOPR AudioSocket Daemon"
---

# Tasks: 2600 Hz Whistle → WOPR AudioSocket Daemon

**Input**: Design documents from `/specs/001-whistle-wopr-daemon/`

**Prerequisites**: plan.md (rev 2.1), spec.md (amended 2026-09-04), research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included. The spec mandates a phone-free test harness (FR-027..FR-031, User Story 1) and
the plan specifies `tests/test_dsp.py` via `unittest`. No pytest.

**Organization**: Tasks are grouped by user story. US1 (harness + detector) is the MVP. Everything
that touches the mini runs over `ssh wopr-mini` (alias in `~/.ssh/config`; `.env.local` has
`MINI=wopr-mini`). Non-interactive SSH lacks Homebrew on PATH: prefix remote commands with
`export PATH=/opt/homebrew/bin:$PATH`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US5)
- Include exact file paths in descriptions

## Path Conventions

Single flat project at repository root (plan.md "Project Structure"): `wopr_daemon.py`,
`harness.py`, `tests/`, `deploy/`, `Makefile`. The daemon is **one file**; tasks that say
"in `wopr_daemon.py`" add a clearly delimited section to that file in the order listed in
plan.md (tunables → DSP → AudioSocket framing → persona/LLM → STT → TTS → CallSession → main).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Turn the empty directory into a runnable project on both machines.

- [X] T001 Initialize git repository at repo root (`git init`, default branch `main`) and create `.gitignore` with `venv/`, `clips/`, `.env.local`, `__pycache__/`, `*.pyc`, `results.json`, `/opt/`-style build artifacts (`deploy/asterisk/build/`)
- [X] T002 [P] Create `requirements.txt` with `faster-whisper>=1.2,<2` and `numpy>=2.0,<3` (research.md R1: cp314 wheels verified)
- [X] T003 [P] Create `Makefile` with targets `sync`, `probe`, `test`, `harness`, `provision`, `verify`, `deploy-daemon`, `logs` per contracts/provisioning.md; read `MINI` from `.env.local` and fail with a clear message if unset; wrap every remote command as `ssh $(MINI) 'export PATH=/opt/homebrew/bin:$$PATH; …'`; `provision` uses `ssh -t`
- [X] T004 [P] Create `README.md` skeleton with sections Overview, Architecture (one paragraph from plan.md Summary), Dev workflow (SSH/Makefile), Running the harness, Provisioning, Tuning, Operations; fill bodies in T075
- [X] T005 [P] Create empty package layout: `tests/__init__.py`, `tests/test_dsp.py` (module docstring only), `deploy/launchd/.gitkeep`, `deploy/asterisk/.gitkeep`, `clips/.gitkeep`
- [X] T006 Create dev-box venv (`python3.14 -m venv venv && venv/bin/pip install -r requirements.txt`) and verify `venv/bin/python -c 'import numpy, faster_whisper'` succeeds; record the ctranslate2 version in README "Dev workflow"
- [X] T007 Run `make sync` then create the mini venv over SSH (`ssh wopr-mini 'export PATH=/opt/homebrew/bin:$PATH; cd ~/CrunchWhistle && python3.14 -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/python -c "import numpy, faster_whisper; print(1)"'`) and confirm output `1`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The daemon's skeleton and the pure-DSP toolbox every story builds on. After this
phase a connection can be accepted, framed, paced, logged, and torn down, but nothing is detected.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T008 Create `wopr_daemon.py` header: module docstring, `from __future__ import annotations`, stdlib imports (`asyncio, argparse, collections, dataclasses, enum, io, json, logging, math, os, struct, sys, time, urllib.request, wave`), `import numpy as np`, and the **TUNABLES** block with every constant named in contracts/daemon-config.md (`GOERTZEL_N=80`, `BINS_HZ=(2500,2600,2700)`, `RATIO_THRESHOLD=0.35`, `MIN_BIN_ENERGY=1e6` placeholder, `HOLD_WINDOWS=30`, `MISS_BUDGET=2`, `REARM_WINDOWS=50`, `ECHO_GUARD_WINDOWS=20`, `WINK_HZ=480`, `WINK_MS=200`, `WINK_AMPLITUDE=0.5`, VAD_*, ESPEAK_*, LLM_*, `HISTORY_MAX_TURNS=20`, `MAX_REPLY_CHARS=280`, `OUTBOUND_QUEUE_FRAMES=1500`, `SAMPLE_RATE=8000`, `FRAME_SAMPLES=160`, `FRAME_BYTES=320`, `WINDOW_SAMPLES=80`, `WINDOW_BYTES=160`)
- [X] T009 [P] Implement DSP section in `wopr_daemon.py`: `goertzel_energy(samples: np.ndarray, k: int, n: int) -> float`, `goertzel_coeffs()` precomputing coefficients for `BINS_HZ` at `GOERTZEL_N`, `window_ratio(win: np.ndarray) -> tuple[float, float]` returning `(ratio, e2600)`, `pcm16_to_f32`, `f32_to_pcm16`, `rms(win)`, `synth_tone(hz, ms, amplitude, sr=8000) -> np.ndarray[int16]`, `fir_lowpass_taps(cutoff_hz, sr, ntaps=64)`, `resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray` (FIR anti-alias when downsampling, `np.interp` always), `frames_from_pcm(pcm: bytes) -> list[bytes]` (pads last frame with zeros to 320 bytes)
- [X] T010 [P] Implement AudioSocket framing section in `wopr_daemon.py`: `FrameType` IntEnum (`TERMINATE=0x00, UUID=0x01, AUDIO=0x10, ERROR=0xFF`), `encode_frame(ftype, payload) -> bytes`, `async read_frame(reader) -> tuple[int, bytes]` using `readexactly(3)` then `readexactly(length)`, `SILENCE_FRAME = encode_frame(AUDIO, b"\x00"*320)`
- [X] T011 [P] Implement logging section in `wopr_daemon.py`: `setup_logging(level: str)` with format `%(asctime)sZ %(levelname)s call=%(call)s %(message)s` (UTC, ms), a `CallLogger(logging.LoggerAdapter)` injecting `call=<uuid8>` (`call=-` for server), helper `kv(**pairs) -> str` producing `event=x k=v` strings, and `Stopwatch` context manager that logs `stage=<name> ms=<elapsed>` at INFO
- [X] T012 Implement `CallState` enum (`ARMED, SPEAKING, CAPTURING, PROCESSING, CLOSED`) and `CallSession` dataclass in `wopr_daemon.py` with every field from data-model.md (`call_id`, `state`, `reader`, `writer`, `inbound_acc: bytearray`, `outbound_q: asyncio.Queue`, `detector`, `echo_ring: deque(maxlen=ECHO_GUARD_WINDOWS)`, `vad`, `utterance: bytearray`, `history: list`, `detected_evt`, `utterance_evt`, `t_start`, `stage_timers`, `log: CallLogger`) plus `set_state(new)` that logs `event=state from= to=` (depends on T008, T011)
- [X] T013 Implement the paced writer task `async def writer_task(sess)` in `wopr_daemon.py`: absolute monotonic deadline loop (`deadline += 0.020; await asyncio.sleep(max(0, deadline - time.monotonic()))`), pops one 320-byte frame from `outbound_q` or sends `SILENCE_FRAME`, calls `sess.note_outbound_window(win)` for each of the two 80-sample windows (no-op stub until T024), `await writer.drain()`, exits cleanly on `CancelledError`/`ConnectionError` (depends on T010, T012)
- [X] T014 Implement the reader task `async def reader_task(sess)` in `wopr_daemon.py`: loop over `read_frame`; `AUDIO` → append to `inbound_acc` and for each full 160-byte window call `sess.process_inbound_window(win)` (stub until T023); `TERMINATE`/EOF/`ConnectionResetError` → raise `CallEnded`; `ERROR` → WARNING `event=proto_error code=`, three consecutive → `CallEnded`; unknown → WARNING `event=unknown_frame type=` (depends on T010, T012)
- [X] T015 Implement `async def handle_call(reader, writer)` in `wopr_daemon.py`: read first frame, reject unless `UUID` with 16-byte payload (`event=bad_handshake`, close), build `CallSession`, log `event=call_start`, run `reader_task`/`writer_task`/`brain_task` (brain is a stub `await asyncio.Event().wait()` until T036) in an `asyncio.TaskGroup`, catch `CallEnded`, `finally`: set `CLOSED`, `writer.close()`, `await writer.wait_closed()` (suppress errors), log `event=call_end dur_ms= turns=` (depends on T013, T014)
- [X] T016 Implement `main()` in `wopr_daemon.py`: `argparse` with `--bind/--port/--log-level/--stub-llm/--ollama-url/--model/--whisper-model/--check` and env fallbacks exactly per contracts/daemon-config.md; `event=config` line printing all tunables; `asyncio.start_server(handle_call, bind, port)`; `event=listen bind= port=`; SIGTERM/SIGINT → graceful shutdown; `--check` path stubbed to print tunables and exit 0 until T033
- [X] T017 [P] Write `tests/test_dsp.py` unit tests (unittest): frame encode/decode round-trip incl. 3-byte header and odd lengths; `frames_from_pcm` padding; `window_ratio` on a synthesized 2600 Hz window ≥ 10, on 2500 Hz and 2700 Hz < 0.35, on digital silence returns `e2600 < MIN_BIN_ENERGY`; `resample` 22050→8000 preserves a 1 kHz tone (peak bin within 5 %) and yields expected length; `synth_tone` length and amplitude
- [X] T018 Run `venv/bin/python -m unittest discover -s tests -v` on the dev box and `make test` on the mini; both must pass before Phase 3

**Checkpoint**: `venv/bin/python wopr_daemon.py --port 9093` accepts a raw AudioSocket connection (e.g. `python3 -c` sending a UUID frame), streams silence at 20 ms cadence, and logs `call_start`/`call_end` on disconnect.

---

## Phase 3: User Story 1 - Whistle detection verified without a phone (Priority: P1) 🎯 MVP

**Goal**: Goertzel detector + wink tone in the daemon, and a harness that synthesizes clips,
replays them over the real protocol, detects the wink in the return audio, and reports
detection rate, false positives, time-to-detect, hangup, and pacing.

**Independent Test**: Terminal A `venv/bin/python wopr_daemon.py --stub-llm --port 9093`;
Terminal B `venv/bin/python harness.py synth && venv/bin/python harness.py run --port 9093`
prints the results table and exits 0 (quickstart.md Scenario 2, minus `bargein`).

### Harness (built first, per spec)

- [X] T019 [P] [US1] Create `harness.py` with argparse subcommands `synth`, `run`, `play` and shared constants; import nothing heavier than numpy/stdlib; import `goertzel_energy`, `synth_tone`, `encode_frame`, `read_frame`, `frames_from_pcm` from `wopr_daemon` (guard: `wopr_daemon` must not import `faster_whisper` at module import — verify with `python -c "import wopr_daemon"` in a venv without it)
- [X] T020 [P] [US1] Implement clip synthesis in `harness.py` per research.md R13: `clean_2600`, `whistle_drift` (2600 ± 30 Hz at 0.5 Hz LFO, ±30 % amplitude at 3 Hz, breath noise at −30 dBFS), `whistle_dropout` (two 10 ms gaps), `short_burst` (150 ms), `speech_only` (espeak-ng default voice via `subprocess`, resampled to 8 kHz, + line noise), `speech_harmonics` (speech + 2500 Hz and 2700 Hz at −20 dBFS), `sweep` (2000→3200 Hz, 2 s), `far_off` (2450 Hz then 2750 Hz); all with 500 ms leading silence; write `clips/<name>.wav` (8 kHz mono s16) and `clips/manifest.json` with `expect_detect`, `onset_ms`, `max_ttd_ms`; deterministic via `--seed`; exit 2 with a warning if `espeak-ng` is missing (skip speech clips)
- [X] T021 [P] [US1] Implement the AudioSocket client in `harness.py`: `async def stream_clip(host, port, wav_path, *, then=None, timeout_s, on_return_window)` that connects, sends a random 16-byte UUID frame, streams 320-byte frames at an absolute 20 ms cadence, concurrently reads return frames and calls `on_return_window` per 80/100-sample window with the frame index, records inter-arrival times, sends TERMINATE at the end, returns a result object (`detected`, `ttd_ms`, `arrival_intervals`, `eof_ms`)
- [X] T022 [US1] Implement wink detection in `harness.py`: Goertzel at 480 Hz with N=100 (bin 6), ≥ 5 consecutive passing windows with ratio to neighbours (380/580 Hz) ≥ 2 and absolute energy floor; first pass index → `ttd_ms = wink_ms - onset_ms`; expose as `WinkDetector` class (depends on T021)

### Detector and wink in the daemon

- [X] T023 [US1] Implement `ToneDetector` class in `wopr_daemon.py` per data-model.md: fields `run`, `misses`, `last_ratio`, `rearm`; `update(win: np.ndarray, masked: bool) -> bool` applying the pass/miss/mask rule, `REARM_WINDOWS` after a fire; DEBUG log `event=window ratio= e26= pass= masked= run=` via the session logger; wire `CallSession.process_inbound_window` to call it and on fire: log `event=detect ratio= run= t_ms=`, set `detected_evt`
- [X] T024 [US1] Implement echo-guard bookkeeping in `wopr_daemon.py`: `CallSession.note_outbound_window(win)` appends the window's pass verdict (same `window_ratio` test) to `echo_ring`; `process_inbound_window` computes `masked = any(echo_ring)` before calling the detector (depends on T023)
- [X] T025 [US1] Implement wink playback in `wopr_daemon.py`: `wink_frames()` = `frames_from_pcm(synth_tone(WINK_HZ, WINK_MS, WINK_AMPLITUDE))` cached at import; minimal `brain_task` for this story: wait `detected_evt`, `set_state(SPEAKING)`, enqueue wink frames, then a 1 s stub greeting placeholder (silence) and return to `ARMED` (`REARM` prevents an immediate re-fire); log `event=play_start`/`play_end`

### Harness runner and report

- [X] T026 [US1] Implement `harness.py run` per contracts/harness-cli.md: iterate manifest × `--repeat`, `--only` filter, per-clip `ClipResult`, `hangup` scenario (whistle → wait for wink → TERMINATE → measure EOF ≤ 1000 ms), `pacing` scenario (p95 ≤ 30 ms, max gap ≤ 100 ms over all runs), aligned text table, `--json PATH` output, summary line with detect rate and false-positive count, exit 0/1/2 (depends on T020–T022)
- [X] T027 [US1] Implement `harness.py play FILE.wav [--then FILE2.wav]` reusing `stream_clip`; print whether/when the wink was heard (depends on T021, T022)
- [X] T028 [US1] Measure and set `MIN_BIN_ENERGY` in `wopr_daemon.py`: run the daemon at `--log-level DEBUG` while `harness.py run --only speech_only,speech_harmonics,clean_2600,whistle_drift`, extract `e26=` values (silence/speech vs whistle), choose a floor ≥ 10× the speech maximum and ≤ 0.1× the whistle minimum; record the numbers in a comment next to the constant
- [X] T029 [US1] Run `harness.py run --port 9093` on the dev box: all clips match expectations, `hangup` and `pacing` pass, exit 0; then `harness.py run --only whistle_drift --repeat 20` ≥ 19/20; fix detector/harness until green
- [X] T030 [US1] Run the same on the mini via `make sync && make harness` (Makefile target starts the daemon on 9093 with `--stub-llm`, runs the harness, stops it); paste the results table into README "Running the harness"

**Checkpoint**: User Story 1 delivers a validated, tunable detector with a regression suite (SC-001..SC-004, SC-009 partial, SC-011 partial).

---

## Phase 4: User Story 2 - Whistle opens a conversation with WOPR (Priority: P2)

**Goal**: After the wink, WOPR greets Professor Falken, then loops: VAD-endpointed capture →
local STT → Ollama (`qwen3:8b`, think off) → espeak-ng croak → paced playback.

**Independent Test**: quickstart.md Scenario 5. `harness.py play clips/clean_2600.wav --then
clips/speech_only.wav --port 9093` against a `--stub-llm` daemon shows the full log sequence
`detect → play_start → play_end → capture_start → utterance → stt → llm (stub) → tts → play_start`;
then the same against a live-Ollama daemon on the mini produces an all-caps WOPR reply ≤ 5 s.

- [X] T031 [P] [US2] Implement the WOPR persona section in `wopr_daemon.py`: `WOPR_SYSTEM_PROMPT` (all-caps, terse, flat affect, addresses "PROFESSOR FALKEN", always steers toward proposing a game incl. global thermonuclear war, never breaks character, ≤ 2 sentences), `GREETING = "GREETINGS PROFESSOR FALKEN."`, `ERROR_LINE`, `STUB_LINES` (≥ 6 rotating lines), and `postprocess_reply(text) -> str` (strip `<think>…</think>`, strip markdown/emoji, uppercase, collapse whitespace, truncate to `MAX_REPLY_CHARS` at a sentence boundary)
- [X] T032 [P] [US2] Implement TTS section in `wopr_daemon.py`: `async def tts_frames(text) -> list[bytes]` running `espeak-ng -v ESPEAK_VOICE -s -p -a --stdout` via `asyncio.create_subprocess_exec` with text on stdin, parse WAV from `BytesIO` with `wave`, `resample(22050→8000)`, `frames_from_pcm`; log `event=tts ms= frames=`; raise `PipelineError("tts")` on non-zero exit
- [X] T033 [P] [US2] Implement STT section in `wopr_daemon.py`: lazy `get_whisper()` singleton (`WhisperModel(name, device="cpu", compute_type="int8")`, import inside the function), `async def transcribe(pcm8k: bytes) -> str` converting int16→float32, `resample(8000→16000)`, `await asyncio.to_thread(model.transcribe, audio, language="en", beam_size=1, vad_filter=False, without_timestamps=True, condition_on_previous_text=False)` under a module-level `asyncio.Semaphore(1)`, join segments; log `event=stt text= ms=`; also finish `--check` (T016) to verify espeak-ng +croak, load Whisper, and (unless stub) GET `/api/tags` contains `--model`
- [X] T034 [P] [US2] Implement Ollama client section in `wopr_daemon.py`: `async def llm_reply(history, user_text, *, url, model) -> tuple[str, dict]` posting the exact body in contracts/daemon-config.md (`stream:false, think:false, keep_alive:-1, options{num_predict,num_ctx,temperature,stop}`) via `urllib.request` in `asyncio.to_thread` with `LLM_TIMEOUT_S`; parse `message.content`, `prompt_eval_duration`, `eval_duration`, `eval_count` (ns→ms); log `event=llm ms= prompt_ms= gen_ms= tokens= chars=`; map `URLError/HTTPError/TimeoutError/JSONDecodeError/KeyError` → `PipelineError("llm")`; `stub_reply()` when `--stub-llm`
- [X] T035 [P] [US2] Implement VAD section in `wopr_daemon.py`: `VadState` dataclass per data-model.md; `update(win) -> Optional[bytes]` maintaining EMA noise floor (init from first 200 ms), start hysteresis (`VAD_START_WINDOWS` above `max(floor*VAD_START_RATIO, VAD_ABS_MIN)`), endpoint after `ENDPOINT_MS` below threshold, discard < `MIN_UTTERANCE_MS`, force-end at `MAX_UTTERANCE_MS`; returns the utterance bytes when complete; wire into `process_inbound_window` only when `state == CAPTURING`, setting `utterance_evt` and logging `event=utterance ms=`
- [X] T036 [US2] Implement the full `brain_task` state machine in `wopr_daemon.py` replacing T025's stub: `ARMED` wait detect → `SPEAKING` (wink + `tts_frames(GREETING)`) → wait until `outbound_q` drains (`await outbound_q.join()` with the writer calling `task_done`) → `CAPTURING` (reset VAD, `event=capture_start`) → on `utterance_evt` `PROCESSING`: `transcribe`, skip empty (`event=empty_utterance`), `llm_reply`, append 2 turns to `history` capped at `HISTORY_MAX_TURNS`, `tts_frames`, enqueue → `SPEAKING` → drain → `CAPTURING`; `PipelineError` → WARNING `event=pipeline_error stage= err=`, enqueue `tts_frames(ERROR_LINE)` (or skip if TTS failed) → `CAPTURING`; every stage wrapped in `Stopwatch` (depends on T031–T035)
- [X] T037 [US2] Implement Ollama warm-up in `wopr_daemon.py` `main()`: unless `--stub-llm`, background task that posts `num_predict:1` every `LLM_WARMUP_RETRY_S` until success, logging `event=llm_warm ms=` / `event=llm_warm_retry`; the server listens regardless (depends on T034)
- [X] T038 [P] [US2] Add unit tests to `tests/test_dsp.py`: `postprocess_reply` (think-block stripping, uppercase, truncation at sentence boundary, markdown removal), `VadState` endpointing on a synthetic burst + 800 ms silence, Ollama response parsing from a canned JSON fixture (ns→ms), `frames_from_pcm` on TTS-length buffers
- [X] T039 [US2] Add `--then` handling and a `conversation` check to `harness.py play`: after the second clip, wait up to 15 s for non-silent return audio and report "reply heard at N ms"; extend `harness.py run` with an optional `--conversation` scenario that requires a reply within 15 s (stub) so US2 has a scripted pass/fail (depends on T027)
- [X] T040 [US2] Dev-box validation: `wopr_daemon.py --stub-llm --port 9093` + `harness.py play clips/clean_2600.wav --then clips/speech_only.wav --port 9093`; confirm the log sequence from quickstart Scenario 5 and that the greeting/STUB reply audio is audible when saved via `harness.py play --save-return out.wav` (add that flag if absent)
- [X] T041 [US2] Mini validation with live Ollama: `make sync`; on the mini ensure `ollama serve` is running (the hand-started one is fine for now) and run `venv/bin/python wopr_daemon.py --port 9093` (no stub) then the same `play --then` from the mini; confirm `llm_warm`, an all-caps reply naming Professor Falken and proposing a game, and utterance→play_start ≤ 5000 ms once warm; record measured `llm ms=` in README "Tuning"; if > 5 s, try `--model llama3.1:8b` after `ollama pull` and record both

**Checkpoint**: User Stories 1 and 2 work independently; a caller can hold a conversation (SC-005 pending real phone, SC-007 measured).

---

## Phase 5: User Story 3 - Second whistle barges in and resets (Priority: P3)

**Goal**: A whistle during playback or processing stops output within 100 ms, clears history,
and restarts with wink + greeting; sidetone never self-triggers; whistle-over-TTS still fires.

**Independent Test**: `harness.py run --only bargein --port 9093` passes (silence ≤ 100 ms after
the second detect, second wink ≤ 1 s); a `selftrigger` scenario streams silence for 20 s after
the first whistle and sees exactly one wink.

- [X] T042 [US3] Implement barge-in handling in `brain_task` in `wopr_daemon.py`: the detector may fire in any non-`ARMED` state; on `detected_evt` while `SPEAKING/CAPTURING/PROCESSING`: drain `outbound_q` (`get_nowait` loop + `task_done`), cancel the in-flight processing coroutine (`asyncio.Task.cancel()` on a child task; discard `to_thread` results via a generation counter), `history.clear()`, log `event=bargein`, restart from wink + greeting; ensure `outbound_q.join()` waiters are released
- [X] T043 [US3] Make the writer drop-safe in `wopr_daemon.py`: after a drain, at most one already-dequeued frame is sent; add a `sess.playback_gen` counter checked by the writer so frames from a cancelled generation are skipped
- [X] T044 [US3] Add a `selftrigger` harness scenario in `harness.py`: stream `clean_2600` then 20 s of silence with a simulated sidetone (mix the return audio back into the outbound stream at −12 dB with 40 ms delay); PASS if exactly one wink is heard; expose `--sidetone-db` and `--sidetone-ms` flags (depends on T021, T022)
- [X] T045 [US3] Add the `bargein` harness scenario in `harness.py` per contracts/harness-cli.md: whistle → wink → wait for ≥ 200 ms non-silent return audio (greeting) → stream `clean_2600` again while reading → PASS if return audio is silent within 100 ms of the second wink-detect *and* a second wink follows within 1 s; include the sidetone mix from T044 so the test is realistic (depends on T044)
- [X] T046 [US3] Add a `whistle_over_tts` harness scenario in `harness.py`: like `bargein` but the second whistle is `whistle_drift` mixed at −6 dB under the sidetone; PASS if detection still occurs (verifies FR-011's "must not disable barge-in") (depends on T045)
- [X] T047 [US3] Run `harness.py run --only bargein,selftrigger,whistle_over_tts --port 9093` on dev box and mini; tune `ECHO_GUARD_WINDOWS` if `selftrigger` fails or `whistle_over_tts` misses; record chosen value and rationale in the tunables comment

**Checkpoint**: SC-006 and SC-008 pass in the harness.

---

## Phase 6: User Story 4 - Clean hangup and unattended operation on the Mac mini (Priority: P4)

**Goal**: Hangup frees everything; the daemon and Ollama run as boot-time LaunchDaemons under
`mattlee` with logs and rotation; the operator can restart, inspect, and profile latency.

**Independent Test**: `harness.py run --only hangup,soak --port 9092` against the installed
service passes (EOF ≤ 1 s; 100 connect/hangup cycles with stable RSS and zero leaked tasks);
`sudo reboot`, no login, `make verify` shows ollama and wopr running and a harness run passes.

- [X] T048 [US4] Add a `soak` harness scenario in `harness.py`: 100 sequential connect → whistle → TERMINATE cycles; PASS if every EOF ≤ 1000 ms and, when the daemon exposes `--stats-port` (see T049), task count returns to baseline and RSS growth < 20 MB
- [X] T049 [US4] Add an optional `--stats-port` to `wopr_daemon.py` serving a one-line JSON (`active_calls`, `tasks`, `rss_mb` via `resource.getrusage`) over a loopback TCP socket for the soak test; off by default
- [X] T050 [US4] Audit teardown paths in `wopr_daemon.py`: hangup during `PROCESSING` cancels the brain's child task; `to_thread` STT result discarded when `state == CLOSED`; no `writer.write` after close (guard in `writer_task`); espeak-ng subprocess killed on cancel; add `tests/test_dsp.py` case for the generation-counter discard logic
- [X] T051 [P] [US4] Create `deploy/launchd/com.crunchwhistle.wopr.plist` per contracts/provisioning.md (`UserName=mattlee`, `GroupName=staff`, `ProgramArguments=[/opt/wopr/venv/bin/python, /opt/wopr/wopr_daemon.py]`, `EnvironmentVariables` incl. `PATH`, `HF_HUB_OFFLINE=1`, `HF_HOME=/opt/wopr/hf`, `WOPR_*` from `/opt/wopr/wopr.env` rendered at provision time, `RunAtLoad`, `KeepAlive`, `ThrottleInterval=5`, `ProcessType=Interactive`, `StandardOutPath/StandardErrorPath=/opt/wopr/log/wopr.log`)
- [X] T052 [P] [US4] Create `deploy/launchd/com.crunchwhistle.ollama.plist` (`/opt/homebrew/bin/ollama serve`, `OLLAMA_HOST=127.0.0.1:11434`, `OLLAMA_MODELS=/Users/<user>/.ollama/models`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_NUM_PARALLEL=1`, same common keys, log `/opt/wopr/log/ollama.log`)
- [X] T053 [P] [US4] Create `deploy/wopr.env.example` (`WOPR_BIND=127.0.0.1`, `WOPR_PORT=9092`, `WOPR_LOG_LEVEL=INFO`, `WOPR_OLLAMA_URL=http://127.0.0.1:11434`, `WOPR_MODEL=qwen3:8b`, `WOPR_WHISPER_MODEL=base.en`) and `deploy/newsyslog-wopr.conf` rotating `/opt/wopr/log/*.log` at 10 MB, 7 generations, compressed
- [X] T054 [US4] Write the daemon/Ollama service steps of `deploy/provision.sh` (skeleton created here, Asterisk steps added in US5): `set -euo pipefail`, `PATH` export, `--preflight` (prints the probe facts), `sudo -v`, create `/opt/wopr{,/log,/hf}` owned by `mattlee`, `brew upgrade ollama` if `< 0.33`, stop any hand-started `ollama serve` on 11434, venv at `/opt/wopr/venv` + `pip install -r`, copy `wopr_daemon.py`, render `wopr.env` → plist env, Whisper cache pre-load (`/opt/wopr/venv/bin/python -c 'from faster_whisper import WhisperModel; WhisperModel("base.en", device="cpu", compute_type="int8")'` with `HF_HOME=/opt/wopr/hf`), `ollama pull $WOPR_MODEL` (no-op), install both plists with `launchctl bootstrap system`, `pmset -a sleep 0 disksleep 0 autorestart 1`, install newsyslog conf; each step idempotent with `SKIP`/`DONE` output; `--verify` for these steps (checks 2, 3, 6, 7, 8, 9 of the contract table minus Asterisk)
- [X] T055 [US4] Add `deploy-daemon` Makefile target (copy `wopr_daemon.py` + `requirements.txt` to `/opt/wopr`, `pip install -r` if changed, `sudo launchctl kickstart -k system/com.crunchwhistle.wopr`) and `logs` target (`tail -f /opt/wopr/log/wopr.log`)
- [X] T056 [US4] Run `make provision` (interactive sudo) with `--skip-asterisk`; then `make verify` (non-Asterisk checks pass); `ssh wopr-mini 'sudo launchctl print system/com.crunchwhistle.wopr | grep state'` shows running; `harness.py run --port 9092` from the mini passes incl. `hangup` and `soak`
- [X] T057 [US4] Reboot test: `ssh wopr-mini 'sudo reboot'`, wait 3 min without logging in, `make verify` and `ssh wopr-mini 'cd ~/CrunchWhistle && venv/bin/python harness.py run --port 9092 --only clean_2600,hangup'` pass; note boot-to-ready time in README "Operations"
- [X] T058 [US4] Verify latency profiling: from one live conversation on the mini, sum `utterance → stt → llm → tts → play_start` `ms=` values from `/opt/wopr/log/wopr.log` and document the breakdown in README "Tuning" (SC-007 evidence)

**Checkpoint**: Daemon and Ollama survive reboot with no login; teardown is clean (SC-009, SC-012 partial pending Asterisk).

---

## Phase 7: User Story 5 - Provision the phone switch on the Mac mini (Priority: P5)

**Goal**: Asterisk 22 built from source on macOS with AudioSocket, running as a LaunchDaemon,
HT802 registered with μ-law only, extension 2600 handing calls to the daemon, plus echo and
milliwatt scratch extensions.

**Independent Test**: quickstart.md Scenario 6 steps 1–4: `pjsip show contacts` shows the
payphone Avail; dialing 2601 echoes, 2602 plays a tone, 2600 reaches WOPR after a whistle.

### Build spike (highest risk — do first in this phase, may run in parallel with Phase 4)

- [X] T059 [US5] Asterisk source-build spike on the mini: `ssh -t wopr-mini`; `brew install jansson libxml2 pkg-config libedit`; download `asterisk-22-current.tar.gz` to `~/asterisk-build`; run the configure line from research.md R7 and `make menuselect.makeopts`; capture any failure verbatim into `specs/001-whistle-wopr-daemon/research.md` R7 "Build log" and adjust flags until `configure` and `menuselect` succeed
- [X] T060 [US5] Continue the spike: `menuselect --enable app_audiosocket --enable res_audiosocket --enable func_uuid --enable chan_pjsip --enable codec_ulaw --disable BUILD_NATIVE`; `make -j10`; `sudo make install` into `/opt/asterisk`; `/opt/asterisk/sbin/asterisk -V`; start in foreground once and confirm `module show like audiosocket` lists 2 modules; if the native build is unrecoverable, stop and switch to the colima fallback in research.md R7/R21 (new tasks would be added via `/speckit-converge`)
- [X] T061 [US5] Create `deploy/asterisk/menuselect.makeopts` from the working spike selection (`menuselect/menuselect --list-options` output trimmed to the enabled set) so rebuilds are reproducible

### Configuration

- [X] T062 [P] [US5] Create `deploy/asterisk/asterisk.conf` (all `ast*dir` under `/opt/asterisk`, `runuser=mattlee`, `rungroup=staff`, `verbose=3`, `[options] transmit_silence=yes`)
- [X] T063 [P] [US5] Create `deploy/asterisk/modules.conf` (`autoload=yes`; `require=res_audiosocket.so`, `require=app_audiosocket.so`, `require=chan_pjsip.so`, `require=func_uuid.so`; `noload` list: `chan_sip.so`, `cdr_*`, `cel_*`, `res_hep*`, `app_voicemail*`, `res_config_*`, `res_calendar*`, `res_phoneprov.so`, `res_pjsip_phoneprov_provider.so`, `res_stasis*`, `res_ari*`)
- [X] T064 [P] [US5] Create `deploy/asterisk/pjsip.conf` per research.md R17 (transport-udp 0.0.0.0:5060; endpoint `payphone` `context=payphone disallow=all allow=ulaw direct_media=no rtp_symmetric=yes force_rport=yes rewrite_contact=yes dtmf_mode=rfc4733 auth=payphone-auth aors=payphone`; `[payphone-auth] auth_type=userpass username=payphone`; `#include pjsip_secret.conf` supplying `password=`; aor `max_contacts=1 qualify_frequency=60`)
- [X] T065 [P] [US5] Create `deploy/asterisk/extensions.conf` (`[general] static=yes writeprotect=yes`; `[payphone]` with `2600` → `NoOp`, `Answer()`, `Set(WOPR_UUID=${UUID()})`, `AudioSocket(${WOPR_UUID},127.0.0.1:9092)`, `Hangup()`; `2601` → `Answer()`,`Echo()`; `2602` → `Answer()`,`Milliwatt()`)
- [X] T066 [P] [US5] Create `deploy/asterisk/rtp.conf` (`rtpstart=10000`, `rtpend=10100`) and `deploy/launchd/com.crunchwhistle.asterisk.plist` (`/opt/asterisk/sbin/asterisk -f -U mattlee -G staff -C /opt/asterisk/etc/asterisk/asterisk.conf`, `ProcessType=Interactive`, common keys, log `/opt/wopr/log/asterisk-launchd.log`)
- [X] T067 [P] [US5] Write `deploy/ht802-settings.md` per research.md R18: FXS Port 1 SIP server/user/auth/password/registration, all vocoder slots PCMU, silence suppression no, VAD no, echo cancellation no, jitter buffer fixed/low, DTMF RFC2833, hook flash disabled, optional Offhook Auto-Dial `2600` delay 0, plus verification commands (`pjsip show contacts`, `pjsip show endpoint payphone`, `core show channels verbose` showing `ulaw`, `pjsip set logger on`)

### Provisioning integration

- [X] T068 [US5] Extend `deploy/provision.sh` with the Asterisk steps: brew build deps, download/verify tarball, configure/menuselect/make/install with `deploy/asterisk/menuselect.makeopts` (skip when `asterisk -V` matches `--asterisk-version`), `make samples` only on first install, copy configs from `deploy/asterisk/`, generate `pjsip_secret.conf` once (`openssl rand -base64 18`, 0600) and print it exactly once with ATA instructions, `socketfilterfw --add/--unblockapp` for the asterisk binary, install and bootstrap the asterisk plist; `--skip-asterisk` flag; `--verify` checks 4, 5 and 10 of the contract table (`module show like audiosocket` = 2, `pjsip show endpoints` lists `payphone`, `dialplan show payphone` lists 2600/2601/2602, harness `clean_2600,hangup` against 9092) (depends on T054, T059–T066)
- [X] T069 [US5] Run `make provision` end to end and `make verify` → `10/10 checks passed`; commit the secret-handling output format to README "Provisioning"
- [X] T070 [US5] Configure the HT802 from `deploy/ht802-settings.md` (manual, on its web UI) and confirm `ssh wopr-mini 'sudo /opt/asterisk/sbin/asterisk -rx "pjsip show contacts"'` shows `payphone` Avail; reboot the ATA and confirm it re-registers within 2 min
- [X] T071 [US5] Payphone acceptance: dial 2601 (echo) and 2602 (tone); during a call `core show channels verbose` shows `ulaw`; dial 2600, whistle → wink → greeting → conversation → whistle barge-in → hangup, watching `make logs`; record any detector misses and, if needed, capture handset audio with a temporary `2603` → `Record(/tmp/handset.wav,,30)` extension and replay it with `harness.py play` to tune `RATIO_THRESHOLD`/`MIN_BIN_ENERGY`/`ECHO_GUARD_WINDOWS`
- [ ] T072 [US5] Unattended and offline checks: reboot without login and lift the handset after 3 min (SC-012); unplug the mini's uplink (LAN only) and repeat a full exchange (SC-013); start the 7-day registration soak note in README "Operations" with the date (SC-014 is checked later)

**Checkpoint**: The payphone reaches WOPR end to end on the real hardware.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T073 [P] Final tunables pass in `wopr_daemon.py`: every constant has a one-line comment with the measured basis (harness run date, real-handset run date); `event=config` output matches contracts/daemon-config.md exactly
- [ ] T074 [P] Add `tests/test_dsp.py` cases for any bug found during US3–US5 tuning (regression lock-in), keep `make test` green on both machines
- [ ] T075 Complete `README.md`: architecture, dev workflow, harness results table, provisioning walkthrough (incl. the one-time secret print), HT802 pointer, tuning notes with measured latencies, operations (`launchctl` commands, log paths, reboot behaviour, colima fallback pointer), and "what's not done" (sentence-streamed TTS, optional Docker dev rig per research.md R21, `_wopr` service-user hardening)
- [ ] T076 Run every quickstart.md scenario in order (0 through 6) and tick them off in a "Validation log" appendix at the bottom of `specs/001-whistle-wopr-daemon/quickstart.md` with dates and results
- [ ] T077 [P] Update `specs/001-whistle-wopr-daemon/checklists/requirements.md` notes and, if any spec assumption proved wrong on the real phone, amend `spec.md` Assumptions accordingly
- [ ] T078 Commit the working tree with a descriptive message (first commit after T001 may be done earlier; this is the milestone commit)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none; T002–T005 parallel; T006/T007 after T002.
- **Foundational (Phase 2)**: after Setup. T009/T010/T011/T017 parallel after T008; T012 → T013/T014 → T015 → T016; T018 last. **Blocks all stories.**
- **US1 (Phase 3)**: after Phase 2. Harness tasks T019–T021 parallel with daemon tasks T023–T025; T022 after T021; T026 after T020–T022 and T025; T028–T030 sequential at the end.
- **US2 (Phase 4)**: after US1 (needs the detector, wink, and harness `play`). T031–T035 parallel; T036 after all five; T037 after T034; T038 parallel; T039–T041 sequential.
- **US3 (Phase 5)**: after US2 (barge-in interrupts TTS). T042 → T043; T044 → T045 → T046 parallel with T042/T043; T047 last.
- **US4 (Phase 6)**: after US2 (installs a conversational daemon); T048–T050 and T051–T053 parallel; T054 after T051–T053; T055 parallel with T054; T056 → T057 → T058.
- **US5 (Phase 7)**: T059–T061 (build spike) can start **as soon as Phase 1 is done** and run in parallel with Phases 3–6; T062–T067 parallel any time; T068 after T054 and T059–T066; T069–T072 sequential and need the real ATA/payphone.
- **Polish (Phase 8)**: after all desired stories.

### User Story Dependencies

- **US1**: Foundational only. **MVP.**
- **US2**: US1.
- **US3**: US2.
- **US4**: US2 (US3 recommended so the installed service is feature-complete).
- **US5**: build spike needs only Phase 1; final acceptance (T070–T072) needs US4 installed.

### Parallel Opportunities

- Phase 2: T009, T010, T011, T017 together.
- Phase 3: harness (T019–T022) and daemon detector (T023–T025) are separate files.
- Phase 4: T031–T035 are five independent sections of the daemon file; T038 alongside.
- Phase 6: three plists + env/newsyslog (T051–T053) together; T048–T050 together.
- Phase 7: T062–T067 are six independent config files; the build spike T059–T060 can overlap with Phases 3–6 on the mini while the dev box works on the daemon.

---

## Parallel Example: User Story 1

```bash
# Harness side (harness.py):
Task: "T019 Create harness.py skeleton with synth/run/play subcommands"
Task: "T020 Implement clip synthesis per research.md R13"
Task: "T021 Implement the AudioSocket client stream_clip"

# Daemon side (wopr_daemon.py), same time:
Task: "T023 Implement ToneDetector"
Task: "T024 Implement echo-guard bookkeeping"
Task: "T025 Implement wink playback and minimal brain_task"

# Then sequentially: T022 → T026 → T027 → T028 → T029 → T030
```

## Parallel Example: Long-lead Asterisk spike

```bash
# On the mini, while the dev box does Phases 3–6:
Task: "T059 Asterisk source-build spike (configure/menuselect)"
Task: "T060 make / install / module check"
Task: "T061 Capture menuselect.makeopts"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 (Setup) → Phase 2 (Foundational).
2. Phase 3 (US1): harness first (T019–T022), then detector/wink (T023–T025), then runner and
   tuning (T026–T030).
3. **STOP and VALIDATE**: `harness.py run` exit 0 on both machines; `whistle_drift --repeat 20` ≥ 19/20.

### Incremental Delivery

1. US1 → validated detector with regression suite.
2. US2 → conversations with stub LLM on the dev box, live `qwen3:8b` on the mini; measure latency.
3. US3 → barge-in and self-trigger guard proven by harness scenarios.
4. US4 → daemon + Ollama as LaunchDaemons; survives reboot; soak clean.
5. US5 → Asterisk built and configured; HT802 registered; payphone reaches WOPR.
6. Polish → README, tuning notes, quickstart validation log, commit.

### Kick off the Asterisk spike early

The build spike (T059–T061) is the largest unknown and needs only Phase 1. Start it on the mini
right after `make sync` works, so a failure surfaces while the daemon is still being written and
the colima fallback (research.md R7/R21) can be scheduled without blocking US1–US4.

---

## Notes

- One daemon file: keep sections in plan.md order and delimited with `# ───── SECTION ─────` banners.
- Never import `faster_whisper` at module top level (harness imports the daemon's DSP helpers).
- All remote commands: `ssh wopr-mini 'export PATH=/opt/homebrew/bin:$PATH; …'`; sudo prompts once per `ssh -t` session.
- Dev daemon on 9093, installed service on 9092. Never run the harness against 9092 with `--stub-llm` expectations once the service is live.
- Commit after each checkpoint at minimum.
- The optional Docker dev-box rig (research.md R21) is **not** scheduled; add it with `/speckit-converge` if wanted.
