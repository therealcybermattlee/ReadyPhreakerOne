# Implementation Plan: 2600 Hz Whistle → WOPR AudioSocket Daemon

**Branch**: `001-whistle-wopr-daemon` | **Date**: 2026-09-04 (rev 2.1, mini probed) | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-whistle-wopr-daemon/spec.md` (amended 2026-09-04: local Ollama LLM, headless Mac mini, Asterisk provisioning in scope)

## Summary

A single-file Python `asyncio` daemon accepts Asterisk AudioSocket connections, runs a Goertzel
2600 Hz detector on every inbound 10 ms window, and on a sustained human whistle plays a 480 Hz
wink and opens a voice loop with WOPR: energy-VAD endpointing → local `faster-whisper` STT →
local LLM via Ollama's HTTP API → `espeak-ng +croak` TTS → 20 ms-paced playback. A second
whistle cuts playback and resets the session. The detector stays live during playback; sidetone
self-trigger is defeated by running the same detector on the *outbound* audio and masking only
inbound windows that coincide with outbound 2600-ish content, so a real whistle over TTS still
fires. A phone-free harness synthesizes WAV clips, replays them as an AudioSocket client at
real-time pace, and treats the wink tone in the return audio as the detection signal. The
harness is built first.

Everything runs on one headless Apple Silicon Mac mini (M4, 16 GB) administered over SSH:
Asterisk (built from source, since no Homebrew formula exists), Ollama, espeak-ng, the daemon,
and all models. Three LaunchDaemons bring the stack up at boot with nobody logged in.

## Verified facts about the mini (probed over SSH 2026-09-04)

| Item | Value |
|---|---|
| SSH | `ssh wopr-mini` (alias in `~/.ssh/config` → `mattlee@10.17.13.134`, key auth working); `.env.local` has `MINI=wopr-mini` |
| Hardware / OS | Apple M4, 10 cores, 16 GB, 265 GB free; macOS 26.6.2 (25G83) |
| Homebrew | 5.1.6 at `/opt/homebrew`; **not on PATH for non-interactive SSH** (shellenv is only in `~/.zprofile`) |
| Python | system 3.9.6; Homebrew `python@3.14` installed. ctranslate2 4.8.2 publishes cp314 macOS-arm64 wheels → 3.14 is usable, no 3.12 needed |
| Ollama | Homebrew formula **0.17.0 already installed** (current formula is 0.33.x); `ollama serve` running **manually** as `mattlee` in a shell, no launch item; model **`qwen3:8b` (5.2 GB) already pulled** to `~/.ollama/models` |
| Also installed | `mlx`, `mlx-c`, `openssl@3`, `sqlite`; casks `iterm2`, `utm` |
| Missing | asterisk, espeak-ng, docker, colima, jansson, libxml2, pkg-config, libedit |
| Xcode CLT | present at `/Library/Developer/CommandLineTools` |
| sudo | **requires a password** (provisioning must run with `ssh -t`) |
| Firewall | disabled, stealth off |
| Power | `sleep 1` but "prevented by powerd"; `autorestart 0`; `womp 1` |
| Ports | 11434 (ollama) listening on loopback; 9092 and 5060 free |
| LaunchDaemons | none of ours yet |

## Technical Context

**Language/Version**: Python 3.14 from Homebrew on the mini (already installed as `python@3.14`;
verified that `ctranslate2` 4.8.2, `numpy` 2.5.2 ship cp314 macOS-arm64 wheels). Not the Xcode
CLT python (3.9). Code floor is 3.11. `audioop` is never imported (removed in 3.13). The dev box
is also 3.14, so both machines run the same interpreter.

**Primary Dependencies**:
- `faster-whisper` (STT, `base.en`, `int8`, CPU/Accelerate) — user-mandated
- `numpy` — transitive via faster-whisper; user-approved for direct use
- **Ollama** (Homebrew formula; 0.17.0 present, upgraded to current 0.33.x by provisioning) serving on `127.0.0.1:11434`. Talked to with stdlib `urllib.request` (no client library). Default model **`qwen3:8b`** (already pulled on the mini) with thinking disabled per request; `llama3.1:8b` as the documented alternative.
- `espeak-ng` (Homebrew, 1.52) with `+croak`
- **Asterisk 22 LTS** built from source with bundled pjproject; modules `chan_pjsip`, `app_audiosocket`, `res_audiosocket`, `func_uuid`, `codec_ulaw`
- Build deps from Homebrew: `jansson libxml2 sqlite openssl@3 pkg-config libedit`
- Stdlib only otherwise. The `anthropic` package is **removed**.

**Storage**: N/A at runtime. Whisper weights cached under the service user's cache dir; Ollama
models under `OLLAMA_MODELS`. Both populated at provisioning time only (FR-039).

**Testing**:
- `harness.py` — end-to-end black-box suite over TCP, run on the mini (and on the dev box against a locally started daemon with `--stub-llm`).
- `tests/test_dsp.py` via `python -m unittest` — pure functions. No pytest.
- Provisioning verification: `deploy/provision.sh --verify` checks each service, module, and model and exits non-zero on any miss.

**Target Platform**: macOS 26.6.2 on Apple Silicon Mac mini M4 (10 cores, 16 GB), headless, SSH only.
Services are `/Library/LaunchDaemons` plists (not LaunchAgents) so they run with no login,
running as user `mattlee` (see research.md R16 for why not a separate service user).

**Project Type**: Single-file network daemon + CLI harness + provisioning scripts/configs.

**Performance Goals**:
- Detection ≤ 500 ms after tone onset.
- Barge-in halts outbound audio ≤ 100 ms after detection.
- Caller-stops-speaking → reply-audio-starts ≤ 5 s typical with the model resident (≈ 0.8 s endpoint + ~0.7 s STT + ~1.5 s LLM for ~40 tokens on M4 + ~0.3 s TTS); ≤ 20 s for the first exchange after idle (model load).
- Boot → payphone reaches WOPR ≤ 3 min.

**Constraints**:
- No network egress at call time (FR-034); daemon connects only to loopback (Asterisk, Ollama).
- Outbound audio paced at one 320-byte frame per 20 ms; never burst.
- Detector live during playback; echo guard must not disable barge-in.
- Single daemon source file; tunables are top-of-file constants.
- Everything needed at call time lives on the mini. Dev work is done over SSH.
- Asterisk must be built from source (no Homebrew formula). This is the largest risk; see R16.

**Scale/Scope**: One payphone, a handful of concurrent calls at most. ~1,000 lines daemon,
~500 lines harness, ~300 lines provisioning, ~150 lines Asterisk config.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified
principles to gate against. Self-imposed gates derived from the feature request:

| Self-imposed gate | Status | Evidence |
|---|---|---|
| Test-first: harness before conversation features | PASS | P1 in spec; task order will follow |
| Simplicity: single daemon file, no framework, no client libs | PASS | Ollama via stdlib HTTP; `anthropic` removed |
| No unapproved dependencies | PASS | numpy approved; Ollama/Asterisk build deps covered by clarification 2026-09-04 |
| Everything on the asset, no cloud at call time | PASS | FR-034; loopback-only design; provisioning pulls models |
| Period aesthetic over fidelity | PASS | espeak-ng +croak, Goertzel, 480 Hz wink |
| Observability with stage timing | PASS | `contracts/daemon-config.md` |

**Post-design re-check**: PASS, unchanged. The provisioning layer adds files but no new runtime
dependencies for the daemon.

## Project Structure

### Documentation (this feature)

```text
specs/001-whistle-wopr-daemon/
├── plan.md              # This file
├── spec.md
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── audiosocket-protocol.md
│   ├── harness-cli.md
│   ├── daemon-config.md
│   └── provisioning.md  # NEW: provision.sh interface, service layout, Asterisk/ATA config contract
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks output
```

### Source Code (repository root)

```text
wopr_daemon.py           # Single-file daemon: tunables → DSP → AudioSocket framing →
                         #   WOPR persona + Ollama client → STT → TTS → CallSession → main()
harness.py               # Phone-free test harness (synth / run / play)
requirements.txt         # faster-whisper, numpy (pinned)
Makefile                 # sync (rsync to mini), test, harness, provision, logs — all over SSH
deploy/
├── provision.sh         # Idempotent, re-runnable macOS provisioning (Homebrew pkgs, Python venv,
│                        #   Asterisk source build, service user, LaunchDaemons, model pulls, --verify)
├── launchd/
│   ├── com.crunchwhistle.asterisk.plist
│   ├── com.crunchwhistle.ollama.plist
│   └── com.crunchwhistle.wopr.plist
├── asterisk/
│   ├── pjsip.conf       # transport + HT802 endpoint/auth/aor, ulaw only
│   ├── extensions.conf  # [payphone] 2600 → AudioSocket
│   ├── rtp.conf         # narrow RTP range for the firewall
│   ├── modules.conf     # noload the noise; require audiosocket/pjsip/func_uuid
│   ├── asterisk.conf    # paths under /opt/asterisk, runuser
│   └── menuselect.makeopts  # module selection for the build
├── wopr.env.example     # WOPR_* settings (no secrets)
└── ht802-settings.md    # ATA web-UI settings, screen by screen
tests/
└── test_dsp.py
clips/                   # generated (gitignored)
README.md
```

**Structure Decision**: Flat single project; the user asked for a single-file daemon. The
`deploy/` tree is the provisioning deliverable (FR-035..FR-040). All commands that touch the
mini are wrapped in `Makefile` targets that run over SSH so the workflow is repeatable.

## Development Workflow (SSH)

- The repo is edited on this workstation and mirrored to the mini with `make sync`
  (`rsync -a --delete --exclude clips --exclude venv . $MINI:~/CrunchWhistle/`).
  `MINI=wopr-mini` is set in `.env.local` (gitignored); the alias lives in `~/.ssh/config`.
- Non-interactive SSH on the mini has no Homebrew on PATH; every remote command is prefixed
  with `export PATH=/opt/homebrew/bin:$PATH` by the Makefile.
- `sudo` on the mini needs a password, so `make provision` uses `ssh -t` and the operator types
  it once; `provision.sh` calls `sudo -v` up front and refreshes the timestamp.
- `make test`, `make harness`, `make provision`, `make verify`, `make logs` run the
  corresponding command on the mini via `ssh $MINI 'cd ~/CrunchWhistle && …'`.
- The daemon is installed by `provision.sh` into `/opt/wopr` (copies `wopr_daemon.py`, builds the
  venv there). Development runs happen from `~/CrunchWhistle` with `--port 9093` so they never
  collide with the installed service on 9092.
- Hardware and software assumptions were verified over SSH on 2026-09-04 (table above); the
  first task re-runs that probe as `provision.sh --preflight` so drift is caught.

## Design Overview

### Per-connection concurrency

`handle_call()` opens an `asyncio.TaskGroup` with three tasks:

1. **reader** — `readexactly(3)` + `readexactly(len)`; dispatches by type; audio goes into an
   accumulator consumed in 160-byte windows (tolerates odd frame sizes). Each window feeds the
   detector and, in `CAPTURING`, the VAD/utterance buffer.
2. **writer** — monotonic-deadline loop sending one 320-byte frame every 20 ms from the outbound
   queue, or silence. Every outbound window is also scored by the detector for the echo guard.
   Only this task writes after the UUID handshake.
3. **brain** — state machine: detection/utterance events → STT → LLM → TTS → enqueue; barge-in
   drains the queue and cancels in-flight work.

Hangup cancels the group; `finally` closes the writer and drops state. `to_thread` STT cannot be
interrupted; its result is discarded (FR-004).

### Detector and echo guard

Unchanged from rev 1 (see research.md R3, R4): Goertzel k = 25/26/27 at N = 80, ratio ≥ 0.35
plus an absolute floor, hold 30 with miss budget 2; outbound-verdict ring of 200 ms masks only
coincident inbound windows.

### Conversation loop

`CAPTURING` energy VAD → 800 ms endpoint → `PROCESSING`: STT in a thread (8 k → 16 k via
`np.interp`, `WhisperModel.transcribe`), empty → back to `CAPTURING`; else POST to Ollama
`/api/chat` (stdlib `urllib` in a thread, 20 s timeout, `stream=false`, `keep_alive=-1`,
`num_predict≈80`, `num_ctx=2048`) with the WOPR system prompt and capped history; post-process
(uppercase, strip markdown, cap ~280 chars); `espeak-ng --stdout` → resample → frames →
`SPEAKING` → `CAPTURING`. Any failure → canned line, back to `CAPTURING`. At daemon start, a
warm-up request loads the model so the first caller doesn't pay the load cost.

### Provisioning (User Story 5 + FR-035..040)

`deploy/provision.sh` runs over SSH as an admin user with sudo and is idempotent:

1. Verify hardware/OS assumptions; `xcode-select --install` if CLT missing.
2. `brew install espeak-ng jansson libxml2 pkg-config libedit` (python@3.14, sqlite, openssl@3
   already present) and `brew upgrade ollama` (0.17.0 → current).
3. Create `/opt/wopr` and `/opt/asterisk` owned by `mattlee`; stop the manually started
   `ollama serve` (pid found by port 11434) so the LaunchDaemon can own the port.
4. Build Asterisk 22 LTS from source into `/opt/asterisk` with bundled pjproject and the
   `menuselect.makeopts` from `deploy/asterisk/`; skip if the installed version matches.
5. Install Asterisk configs from `deploy/asterisk/` (HT802 secret generated once, stored 0600).
6. Create venv in `/opt/wopr/venv`, `pip install -r requirements.txt`, copy `wopr_daemon.py`.
7. Models: `ollama pull qwen3:8b` is a no-op (already present in `~/.ollama/models`); a Python
   one-liner instantiates `WhisperModel("base.en")` so the Hugging Face cache under
   `/opt/wopr/hf` is populated.
8. Install the three LaunchDaemon plists, `launchctl bootstrap system …`, ordering by
   `KeepAlive` + the daemon's own retry-until-Ollama-and-Asterisk-are-up logic (no launchd
   dependency graph exists; the daemon and Asterisk simply retry).
9. `--verify`: each service running, `asterisk -rx 'module show like audiosocket'` shows 2
   modules, `pjsip show endpoints` lists `payphone`, Ollama answers `/api/tags` with the model,
   `wopr_daemon.py --check` passes, exits 0.

The HT802 is configured by hand from `deploy/ht802-settings.md` (spec assumption).

### Harness

Unchanged from rev 1 (research.md R12, R13, `contracts/harness-cli.md`). Runs on the mini
against the dev daemon on port 9093, or on the dev box.

## Complexity Tracking

No constitution violations. Additions beyond the literal spec, with justification:

| Addition | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Absolute energy floor in the detector | Ratio of near-zero bins on silence is meaningless | Pure ratio would fire on silence, failing SC-003 |
| Outbound-verdict ring echo guard | Keeps barge-in live during continuous TTS | Fixed suppression stays active for the whole reply, which the spec forbids |
| `--stub-llm` flag | Harness must run with Ollama stopped | Otherwise detector tests depend on a 5 GB model being resident |
| Asterisk source build script | No Homebrew formula exists (verified 2026-09-04) | Docker/VM needs a login session or a second OS to manage on a headless mini |
| LaunchDaemons with `UserName=mattlee` | FR-038 headless boot without a login session | LaunchAgents only run after login; a separate `_wopr` user would orphan the existing 5 GB model store and needs extra sudo steps on a box where sudo prompts (optional hardening later) |
