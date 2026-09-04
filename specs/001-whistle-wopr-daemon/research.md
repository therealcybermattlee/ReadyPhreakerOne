# Research: 2600 Hz Whistle → WOPR AudioSocket Daemon (rev 2)

**Date**: 2026-09-04 | **Plan**: [plan.md](plan.md)

Rev 2 supersedes rev 1 after the spec amendment (local Ollama LLM, headless Mac mini M4 16 GB,
Asterisk provisioning in scope). Entries R1–R6, R8, R11–R14 carry over with edits; R7, R9, R15
are rewritten; R16–R20 are new. Verified facts are marked **[verified]**; everything else is a
decision to be confirmed by the first provisioning task on the mini.

---

## R1. Python version on the mini

**Decision**: Homebrew `python@3.14`, which is **[verified]** already installed on the mini and is
also the dev box's version. Floor 3.11 in code. Never import `audioop`.

**Rationale**: macOS ships 3.9 via the Xcode CLT, below the `asyncio.TaskGroup` floor.
**[verified 2026-09-04 on PyPI]** `ctranslate2` 4.8.2 and `numpy` 2.5.2 publish cp314
macOS-arm64 wheels and `faster-whisper` 1.2.1 is pure Python, so 3.14 has no wheel gap.
One interpreter on both machines removes a whole class of "works here" bugs.

**Alternatives**: `python@3.12` (rev 2 choice; unnecessary now); system python (too old).

---

## R2. Resampling (22050 → 8000 Hz TTS; 8000 → 16000 Hz STT)

**Decision**: numpy (user-approved). Downsample: windowed-sinc FIR low-pass (~64 taps, cutoff
3.4 kHz) via `np.convolve` then `np.interp`. Upsample: `np.interp`.

**Rationale**: Vectorized, no subprocess, no new install. **[verified]** `audioop` absent on 3.14.

**Alternatives**: pure-Python FIR (slow); `sox` pipe (extra package, extra subprocess);
espeak-ng native 8 kHz (CLI has no rate option).

---

## R3. Goertzel parameters and drift tolerance

**Decision**: N = 80 at 8 kHz → 100 Hz bins; k = 25/26/27 land exactly on 2500/2600/2700 Hz.
Pass ≡ `E26/(E25+E27+eps) ≥ 0.35` **and** `E26 ≥ MIN_BIN_ENERGY`. Hold 30, miss budget 2,
re-arm 50 windows after a fire.

**Rationale**: Rectangular-window leakage gives ratio ≈ 4.3 at ±30 Hz and ≈ 0.9 at ±50 Hz, so
the ±30 Hz requirement has >10× margin over the threshold. The absolute floor is mandatory:
on digital silence all bins ≈ 0 and the ratio is noise. Hold, not threshold, rejects speech.

**Alternatives**: Hann window (wider lobe lowers ratio at nominal); FFT (user mandated Goertzel).

---

## R4. Self-trigger guard without disabling barge-in

**Decision**: Score every outbound 80-sample window with the same pass test; keep a ring of the
last `ECHO_GUARD_WINDOWS = 20` verdicts (200 ms); an inbound window is *masked* (run frozen) if
any verdict in the ring passed.

**Rationale**: Only outbound content that itself passes the 2600 test can fool the detector;
the wink is 480 Hz and croak speech rarely holds 2600 Hz for 300 ms, so the mask is nearly
always off and a genuine whistle over TTS still fires.

**Alternatives**: fixed post-emission suppression (continuously renewed during TTS = barge-in
disabled, forbidden); NLMS echo cancellation (re-adds what the user turned off on the ATA);
2600 Hz notch on the TTS path (kept as an optional knob).

---

## R5. Real-time pacing of outbound audio

**Decision**: One writer task per call on an absolute monotonic deadline; one 320-byte frame per
20 ms tick, silence when idle.

**Rationale**: Bursting overruns Asterisk's jitter buffer and makes barge-in impossible because
audio already inside Asterisk can't be recalled. Strict pacing keeps ≤ 1 frame in flight.

---

## R6. AudioSocket protocol handling

**Decision**: As in `contracts/audiosocket-protocol.md`: UUID must be first; audio of any
length accumulates into 160-byte windows; `0x00`/EOF/reset → close; `0xff` logged, three in a
row → close; unknown types ignored. Daemon never sends `0x00`; it closes the socket.

---

## R7. Asterisk on macOS: source build (Homebrew has no formula)

**[verified 2026-09-04]** `brew info asterisk` → "No available formula"; formulae.brew.sh
returns 404. Build deps `jansson libxml2 sqlite openssl@3 pkg-config libedit` all exist in
Homebrew; `uuid` is provided by libSystem on macOS. `asterisk-22-current.tar.gz` is published.

**Decision**: Build **Asterisk 22 LTS** from source into prefix `/opt/asterisk`, bundled
pjproject, run by LaunchDaemon as `mattlee`. Configure line (to be confirmed on the mini):

```
./configure --prefix=/opt/asterisk --with-pjproject-bundled --with-jansson=$(brew --prefix jansson) \
  --with-ssl=$(brew --prefix openssl@3) --with-libxml2=$(brew --prefix libxml2) \
  --with-sqlite3=$(brew --prefix sqlite) --with-libedit=$(brew --prefix libedit) \
  CFLAGS="-I$(brew --prefix)/include" LDFLAGS="-L$(brew --prefix)/lib"
make menuselect.makeopts && menuselect/menuselect --enable app_audiosocket --enable res_audiosocket \
  --enable func_uuid --enable chan_pjsip --enable codec_ulaw --disable BUILD_NATIVE menuselect.makeopts
make -j$(sysctl -n hw.ncpu) && sudo make install && sudo make samples   # samples only on first install
```

`BUILD_NATIVE` is disabled to avoid `-march=native` issues with Apple clang. Minimal
`modules.conf` with `autoload=yes` plus `noload` for chan_sip-era, CDR, and database modules to
cut startup noise and macOS-incompatible modules.

**Rationale**: The user mandated Asterisk on the mini. Source build is the only native route.
Asterisk has a Darwin target in its build system and 20/22 are reported to build on Apple Silicon
with bundled pjproject; residual risk is in individual modules, hence the explicit
`menuselect` list and `--verify` step that checks `module show like audiosocket`.

**Alternatives considered**: Asterisk in Docker (Docker Desktop needs a GUI login; not headless);
Linux VM via colima/lima under a LaunchDaemon (works but adds a second OS to administer; kept as
the documented fallback if the native build fails); FreeSWITCH (no formula either, and the user
said Asterisk); MacPorts `asterisk` port (exists historically but mixes package managers;
second fallback).

**Build log (2026-09-04, Asterisk 22.11.0 on macOS 26.6.2 / M4)**: `configure` with
`--with-pjproject-bundled --with-jansson-bundled --with-ssl=$(brew --prefix openssl@3)` succeeds
unchanged. `make` compiles all modules (app_audiosocket, res_audiosocket, chan_pjsip, func_uuid
included) but fails linking `main/libasteriskpj.dylib`: `main/Makefile` hard-codes
`PJ_TARGET := aarch64-apple-darwin25.2.0` in its Darwin branch while the bundled pjproject
builds under the real host triplet (`aarch64-apple-darwin25.6.0`). Fix: after `configure`,
`sed -i "" "s/^PJ_TARGET := .*/PJ_TARGET := $(./config.guess)/" main/Makefile`. Baked into
`provision.sh`. (Upstream bug worth reporting.) Two more modules fail on macOS and are disabled
in menuselect: `res_geolocation` (embeds XML with `ld -r -b binary`, unsupported by Apple ld) and
`res_http_websocket` (Linux-only `TCP_KEEPIDLE`), plus their dependents
`res_pjsip_transport_websocket` and `chan_websocket`. With those four off, `make -j10` completes
(≈ 12 min on the M4) and `res_audiosocket.so`, `app_audiosocket.so`, `chan_pjsip.so`,
`func_uuid.so`, `codec_ulaw.so` all build. The resulting `menuselect.makeopts` is committed as
`deploy/asterisk/menuselect.makeopts`.

**Risk handling**: The very first task on the mini after hardware verification is a build spike.
If it fails, the plan's fallback is colima + the official `asterisk` Linux container image with
host networking, started by a LaunchDaemon running `colima start` as the admin user.

---

## R8. STT with faster-whisper on Apple Silicon

**Decision**: `WhisperModel("base.en", device="cpu", compute_type="int8")`, loaded once at
startup (lazily under `--stub-llm`), `transcribe(audio16k, language="en", beam_size=1,
vad_filter=False, without_timestamps=True, condition_on_previous_text=False)` in
`asyncio.to_thread`, serialized by a `Semaphore(1)`. Input: float32 numpy at 16 kHz.

**Rationale**: `ctranslate2` uses Accelerate on arm64; `base.en` int8 transcribes a 3 s
utterance in well under a second on M4. Passing an array avoids temp files. Model files are
pulled at provisioning (FR-039) into `/opt/wopr/hf`; `HF_HUB_OFFLINE=1` is set in the
service env so the daemon never tries the network at call time (FR-034).

**Alternatives**: `whisper.cpp` (CoreML-accelerated, but a new dependency the user didn't
list); `tiny.en` (kept as a knob); MLX whisper (new dependency).

---

## R9. LLM via Ollama (replaces Anthropic API)

**[verified 2026-09-04]** Homebrew `ollama` formula exists (0.33.2). On the mini, Ollama
**0.17.0** from that formula is already installed and `ollama serve` is running by hand as
`mattlee` (no launch item), with **`qwen3:8b` (5.2 GB) already pulled** to `~/.ollama/models`.

**Decision**:
- Runtime: `brew upgrade ollama`, then `ollama serve` as a LaunchDaemon (own plist, not
  `brew services`) running as `mattlee` with explicit env: `OLLAMA_HOST=127.0.0.1:11434`,
  `OLLAMA_MODELS=/Users/mattlee/.ollama/models` (keep the existing store),
  `OLLAMA_KEEP_ALIVE=-1` (model stays resident), `OLLAMA_NUM_PARALLEL=1`. Provisioning stops the
  hand-started instance first.
- Model: **`qwen3:8b`**, because it is already on the asset (no 5 GB download, honours
  "everything lives on the mini"). Qwen3 is a hybrid-reasoning model; the request sets
  `"think": false` so it answers directly instead of emitting a reasoning block, which would add
  seconds of latency and leak `<think>` text into TTS. Documented alternative: `llama3.1:8b`
  (pulled only if the operator asks). Either fits 16 GB with headroom.
- Client: stdlib `urllib.request` in `asyncio.to_thread`, `POST /api/chat`, body
  `{"model", "messages": [{"role":"system",...}, ...history, {"role":"user",...}],
  "stream": false, "think": false, "keep_alive": -1, "options": {"num_predict": 80,
  "num_ctx": 2048, "temperature": 0.7, "stop": ["\n\n"]}}`, timeout 20 s. The post-processor
  also strips any `<think>…</think>` block defensively. Response `message.content` plus
  `prompt_eval_duration` / `eval_duration` / `eval_count` (nanoseconds) go straight into the
  `llm` stage log line.
- Warm-up: at daemon start, one request with `num_predict: 1` loads the model so the first
  caller doesn't pay ~10 s of load. Retried until Ollama is up (boot ordering).
- Errors (`URLError`, `HTTPError`, `TimeoutError`, JSON decode) → canned WOPR line, back to
  `CAPTURING` (FR-020).
- Persona: system prompt enforces all-caps, terse, "Professor Falken", game proposals; output
  post-processed (uppercase, strip markdown/emoji, collapse whitespace, cap 280 chars at a
  sentence boundary) because 8B models drift on formatting.
- `--stub-llm` bypasses the HTTP call with canned lines (hermetic harness runs).

**Rationale**: Ollama is the user's choice; its API is trivial enough that a client library adds
nothing. Keeping the model resident is what makes SC-007's 5 s budget achievable. On 16 GB the
8B Q4 model plus Whisper base int8 plus macOS leaves ≈ 6–7 GB headroom.

**Alternatives**: `ollama` Python package (unneeded dependency); OpenAI-compatible `/v1/chat/completions`
endpoint (works too, but `/api/chat` returns timing fields we want); streaming tokens into TTS
per sentence (latency win; deferred to a follow-up because it complicates barge-in cancellation).

---

## R10. TTS with espeak-ng +croak

**[verified]** Homebrew `espeak-ng` 1.52.0 exists; `+croak` variant ships in `espeak-ng-data/voices/!v/`.

**Decision**: `espeak-ng -v en-us+croak -s 140 -p 30 -a 150 --stdout` via
`asyncio.create_subprocess_exec`, text on stdin; parse WAV (22050 Hz mono s16) with `wave` from
`BytesIO`; resample per R2; frame; enqueue. Startup check synthesizes one word and fails fast.

---

## R11. Energy VAD and endpointing

**Decision**: Per-window RMS vs. adaptive noise floor (EMA during non-speech). Start after 3
windows > max(floor×3, `VAD_ABS_MIN`); end after 800 ms below; min 300 ms; max 15 s. Runs only
in `CAPTURING`.

---

## R12. Harness detection signal

**Decision**: Detect the daemon's 480 Hz wink in the return audio (Goertzel N = 100 → bin 6 is
exactly 480 Hz; ≥ 5 consecutive passes). Time-to-detect = wink frame − clip tone onset.
Fully black-box over the real protocol.

---

## R13. Clip corpus

Unchanged: `clean_2600`, `whistle_drift` (±30 Hz, ±30 % amplitude, breath noise),
`whistle_dropout`, `short_burst`, `speech_only` (espeak-ng default voice), `speech_harmonics`
(+2500/2700 Hz tones), `sweep`, `far_off`; scenarios `bargein`, `hangup`, `pacing`. All 8 kHz
mono s16 with 500 ms leading silence.

---

## R14. Logging

**Decision**: stdlib `logging`, key=value lines, `call=<uuid8>`, ISO-8601 UTC. Under launchd,
stdout/stderr go to `/opt/wopr/log/wopr.log` via `StandardOutPath`/`StandardErrorPath` (there is
no journald); `newsyslog` rotation entry in `/etc/newsyslog.d/wopr.conf` installed by
`provision.sh`. `llm` stage lines include Ollama's own `prompt_ms`/`gen_ms`/`tokens`.

---

## R15. Configuration surface (rev 2)

**Decision**: CLI flags with env fallbacks: `--bind`/`WOPR_BIND` (127.0.0.1), `--port`/`WOPR_PORT`
(9092), `--log-level`/`WOPR_LOG_LEVEL`, `--stub-llm`/`WOPR_STUB_LLM`, `--ollama-url`/`WOPR_OLLAMA_URL`
(`http://127.0.0.1:11434`), `--model`/`WOPR_MODEL` (`qwen3:8b`), `--whisper-model`/`WOPR_WHISPER_MODEL`
(`base.en`), `--check`. No secrets anywhere. Detector/VAD/TTS/LLM tunables are top-of-file
constants. `ANTHROPIC_API_KEY` is gone.

---

## R16. Boot-time services on headless macOS

**Decision**: Three plists in `/Library/LaunchDaemons/` (`com.crunchwhistle.{ollama,asterisk,wopr}.plist`),
`RunAtLoad=true`, `KeepAlive=true`, `UserName=mattlee`, `WorkingDirectory`, explicit
`EnvironmentVariables`, `StandardOutPath`/`StandardErrorPath` under `/opt/wopr/log/`,
`ProcessType=Interactive` for asterisk and wopr (real-time audio; avoids App Nap-style
throttling of background processes), `ThrottleInterval=5`. Loaded with
`sudo launchctl bootstrap system <plist>`; managed with `launchctl kickstart -k system/<label>`.

Ordering: launchd has no dependency graph. Asterisk starts independently; the daemon retries
its Ollama warm-up every 2 s until success and listens regardless (AudioSocket connections
before Ollama is warm simply get slower first replies). Asterisk's `AudioSocket()` failing to
connect (daemon not up yet) just ends that call; `KeepAlive` restarts crashed services.

**Rationale**: LaunchAgents require a logged-in user (FR-038 forbids). A LaunchDaemon with
`UserName=mattlee` runs at boot with no session and as a non-root user; Asterisk binds UDP
5060/RTP as non-root fine. Running as `mattlee` (rev 2.1 change) keeps the existing Ollama
model store and Homebrew ownership intact and avoids extra sudo choreography on a box where
sudo prompts for a password. `brew services` with sudo would also produce LaunchDaemons but runs
as root by default and hides the env; explicit plists are clearer and are the versioned
deliverable.

**Alternatives**: dedicated hidden `_wopr` service user (rev 2 choice; better isolation, kept as
optional hardening once the stack works); `brew services` (rejected as above).

---

## R17. Asterisk configuration for the HT802

**Decision** (files in `deploy/asterisk/`):

- `pjsip.conf`: `[transport-udp]` bind `0.0.0.0:5060`; endpoint `payphone` with
  `context=payphone`, `disallow=all`, `allow=ulaw`, `direct_media=no`, `rtp_symmetric=yes`,
  `force_rport=yes`, `rewrite_contact=yes`, `dtmf_mode=rfc4733`, `auth=payphone-auth`,
  `aors=payphone`; `[payphone-auth]` `auth_type=userpass`, `username=payphone`,
  `password=<generated at provision, 0600>`; `[payphone]` aor `max_contacts=1`,
  `qualify_frequency=60` (keeps NAT-less LAN registration healthy and gives SC-014 evidence).
- `extensions.conf`:

  ```
  [payphone]
  exten => 2600,1,NoOp(CrunchWhistle: 2600 -> WOPR)
   same => n,Answer()
   same => n,Set(WOPR_UUID=${UUID()})
   same => n,AudioSocket(${WOPR_UUID},127.0.0.1:9092)
   same => n,Hangup()
  exten => 2601,1,Answer()          ; scratch: echo test for Story 5 acceptance
   same => n,Echo()
  exten => 2602,1,Answer()          ; scratch: milliwatt test tone
   same => n,Milliwatt()
  ```
- `rtp.conf`: `rtpstart=10000`, `rtpend=10100`.
- `modules.conf`: `autoload=yes`, `require=res_audiosocket.so`, `require=app_audiosocket.so`,
  `require=chan_pjsip.so`, `require=func_uuid.so`; `noload` for `chan_sip`, CDR/CEL backends,
  `res_pjsip_*` you don't need, `res_hep*`, `app_voicemail*`, `res_config_*`.
- `asterisk.conf`: `astetcdir`/`astvarlibdir`/`astlogdir`/`astrundir` under `/opt/asterisk`,
  `runuser=mattlee`, `rungroup=staff`, `verbose=3`.

**Rationale**: Minimal PJSIP with μ-law only satisfies FR-036 and the codec success criterion;
`require=` makes a missing AudioSocket module a hard startup failure that `--verify` catches.

---

## R18. HT802 settings (documented, applied by hand)

**Decision**: `deploy/ht802-settings.md` covers, for FXS Port 1: SIP server = mini's LAN IP,
SIP user/auth id `payphone`, password from provisioning output, register = yes, register
expiration 60 s; codec preferences PCMU only (all vocoder slots = PCMU); silence suppression =
no; VAD = no; echo cancellation = no (Advanced/FXS port page); jitter buffer = fixed/low;
DTMF = RFC2833; hook flash = disabled; optional "Offhook Auto-Dial" = `2600` with
"Offhook Auto-Dial Delay" 0 so lifting the handset dials WOPR directly; caller ID/ring
settings for a coin phone (ring voltage/frequency defaults are fine for a Western Electric set).
Also a "how to verify" section using `asterisk -rx "pjsip show contacts"` and
`pjsip set logger on`.

**Rationale**: Spec assumption says ATA config is manual; the deliverable must still be
complete enough that an operator needs nothing else (SC-010).

---

## R19. macOS host details that bite

- **Firewall**: **[verified]** currently disabled. `provision.sh` still adds
  `/opt/asterisk/sbin/asterisk` via `socketfilterfw --add`/`--unblockapp` so enabling the
  firewall later doesn't break SIP; the daemon and Ollama are loopback-only.
- **Sleep**: **[verified]** `sleep 1` (prevented by powerd for now), `autorestart 0`.
  `provision.sh` sets `pmset -a sleep 0 disksleep 0 autorestart 1` so a headless mini keeps
  serving and comes back after power loss.
- **Homebrew PATH**: **[verified]** `brew` is not on PATH for non-interactive SSH (only
  `~/.zprofile` has shellenv). Every remote command and every plist sets
  `PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin` explicitly.
- **sudo**: **[verified]** password required. `make provision` uses `ssh -t`; `provision.sh`
  runs `sudo -v` first. No NOPASSWD rule is added.
- **Real-time audio**: no special entitlement needed for network audio; `ProcessType=Interactive`
  avoids background QoS throttling of the 20 ms pacing loop.
- **Xcode CLT**: **[verified]** present at `/Library/Developer/CommandLineTools`; `provision.sh`
  still checks `xcode-select -p`.
- **Homebrew prefix** `/opt/homebrew` on Apple Silicon; all paths use `$(brew --prefix)`.
- **Model downloads**: only in `provision.sh`; `HF_HUB_OFFLINE=1` in the daemon's env afterwards.

---

## R20. Dev-box vs. mini split

**Decision**: The DSP unit tests and the harness run on both (numpy-only). The STT/LLM/TTS path
and everything Asterisk run only on the mini. `make sync` mirrors the tree over SSH to
`wopr-mini:~/CrunchWhistle`; all mini-side commands are Makefile targets. **[verified]** the
mini has `git` and `rsync`. The git repo is initialized on the dev box in the first task (the
directory is not a repo today). SSH alias `wopr-mini` bypasses the 1Password agent
(`IdentityAgent none`, `IdentitiesOnly yes`), which otherwise trips the mini's auth-attempt limit.

---

## R21. Where Docker helps (and where it doesn't) on this project

User question 2026-09-04: "can we use docker in any beneficial way?"

**Decision**: Docker stays **off the production call path** on the mini. It is offered as an
**optional dev-box integration test rig** and remains the documented **fallback** for Asterisk
if the native build fails.

**Why not on the mini's call path**:
- Docker Desktop and OrbStack need a logged-in GUI session; the mini is headless (FR-038).
  The headless option is colima/lima (Linux VM via Virtualization.framework), which is a second
  OS to patch and reason about.
- Ollama inside a Linux VM loses Metal. An 8B model on CPU in a VM is several times slower than
  native, which breaks SC-007. Ollama must stay native regardless.
- Asterisk in a VM needs SIP/RTP reachability from the ATA. colima's default NAT breaks RTP;
  `--network-address` (bridged vmnet, needs sudo and socket_vmnet) fixes it but then the daemon
  can no longer bind loopback, weakening FR-034's "loopback only" posture.
- The daemon itself gains nothing: it is one Python file with native deps already on the host.

**Where it is beneficial**:
1. **Dev-box integration test of the real Asterisk dialplan** (optional, recommended): a
   `docker compose` with an Asterisk 22 Linux image mounting `deploy/asterisk/*.conf`, plus a
   scripted SIP caller (e.g. `pjsua` CLI or `sipp` in a second container) that dials `2600` and
   plays `clips/*.wav`. This validates `pjsip.conf`, `extensions.conf`, `UUID()`, and the real
   `AudioSocket()` application against the daemon before anything touches the mini, and gives a
   CI-able regression. The daemon runs on the host with `--bind 0.0.0.0 --port 9093`, and the
   container reaches it via `host.docker.internal`. Adds `deploy/dev-compose/` and no runtime
   dependency. Requires Docker on the dev box.
2. **Asterisk fallback on the mini** (already in R7): colima + the same Asterisk image with
   `--network-address` if the macOS source build cannot be made to work.
3. **Reproducible config linting**: `asterisk -rx "dialplan show"` and `pjsip show endpoints`
   inside the container as a pre-provision check of the config files.

**Not beneficial**: building Asterisk for macOS (Docker cannot produce macOS binaries);
packaging the daemon; running Whisper (fine on CPU, but no reason to containerize).

**Action**: If the user wants benefit 1, `/speckit-tasks` adds a `deploy/dev-compose/` task
group after the harness tasks. Otherwise nothing changes.

---

## Open items carried into tasks (not blocking)

- Confirm the Asterisk 22 configure/menuselect line on the mini (build spike is the first
  provisioning task; colima fallback documented in R7).
- Measure `MIN_BIN_ENERGY` and `VAD_ABS_MIN` from the harness corpus and a real handset capture.
- Confirm sidetone delay on the HT802 and adjust `ECHO_GUARD_WINDOWS` if > 200 ms.
- Measure `qwen3:8b` (think off) reply latency on the M4; try `llama3.1:8b` or a 3–4B model if
  the 5 s budget is missed.
- Sentence-streamed TTS as a latency follow-up.
- Decide whether to add the optional Docker dev-box integration rig (R21).
