# CrunchWhistle — 2600 Hz whistle → WOPR

A phone-phreaking homage. Lift the handset of a coin-op payphone, hear nothing but line silence,
whistle 2600 Hz, and the line *winks*. Then a flat, croaking computer voice says
**GREETINGS PROFESSOR FALKEN** and WOPR from *WarGames* wants to play a game.

Everything runs on one headless Apple Silicon Mac mini: Asterisk (built from source), the
AudioSocket daemon, local speech recognition, a Votrax-style voice, and a local LLM. Nothing in
the call path leaves the machine.

## Architecture

```
payphone ──FXS── Grandstream HT802 ──SIP/RTP (μ-law)── Asterisk ──AudioSocket (slin 8 kHz)── wopr_daemon.py
                                                                                              ├─ Goertzel 2600 Hz detector (10 ms windows)
                                                                                              ├─ 480 Hz wink
                                                                                              ├─ energy VAD → faster-whisper (base.en, int8)
                                                                                              ├─ Ollama /api/chat (qwen3:8b, think off)
                                                                                              └─ espeak-ng +croak → 8 kHz, 20 ms-paced playback
```

- `wopr_daemon.py` — the whole daemon, one file, `asyncio`, one `TaskGroup` per call
  (reader / paced writer / brain). Tunables are constants at the top.
- `harness.py` — phone-free test harness. Synthesizes 8 kHz clips, streams them over a real
  AudioSocket connection at 20 ms pace, and detects the daemon's wink in the return audio.
- `tests/test_dsp.py` — unit tests for framing, Goertzel, detector hold/miss logic, VAD,
  persona post-processing, Ollama parsing, resampling, teardown.
- `deploy/` — `provision.sh`, three LaunchDaemons, Asterisk configs, HT802 settings.
- `specs/001-whistle-wopr-daemon/` — spec, plan, research, contracts, tasks.

### How detection works

Every 10 ms window (80 samples) gets three Goertzel bins at exactly 2500 / 2600 / 2700 Hz.
A window *passes* when all three hold:

| Gate | Test | Why |
|---|---|---|
| ratio | `E2600 / (E2500 + E2700) ≥ 0.35` | level-independent; the user-specified test |
| floor | `E2600 ≥ 1e8` | silence and hiss give meaningless ratios |
| purity | `E2600 / total window energy ≥ 0.10` | a loud far-off pure tone leaks into all three bins about equally (ratio ≈ 0.5) and would otherwise pass |

30 consecutive passing windows (300 ms) with a budget of 2 dropouts fire a detection. After a
fire the detector re-arms only once the tone has been absent for 100 ms, so one long whistle
fires once. The rectangular window's leakage keeps a ±30 Hz whistle at ratio ≈ 4 and purity ≈ 0.37.

**Sidetone guard**: the writer scores every *outbound* window with the same test and keeps the
last 200 ms of verdicts. An inbound window is masked (neither pass nor miss) only while an
outbound window that itself looked like 2600 Hz is inside that ring. Croak speech and the 480 Hz
wink never pass the purity gate, so the mask is almost never active and a real whistle over TTS
still barges in.

## Dev workflow

The code is edited on a workstation and mirrored to the mini over SSH. Put the SSH alias in
`.env.local`:

```
MINI=wopr-mini
```

`~/.ssh/config` needs a host entry that bypasses any SSH agent that offers many keys
(`IdentitiesOnly yes`, `IdentityAgent none`). Non-interactive SSH on the mini has no Homebrew on
PATH; every Makefile target exports it.

| Target | Does |
|---|---|
| `make sync` | rsync the tree to `~/CrunchWhistle` on the mini |
| `make probe` | read-only hardware/software facts |
| `make test` / `make local-test` | unit tests on the mini / here |
| `make harness` / `make local-harness` | start a stub daemon on 9093 (under `caffeinate`), synth clips, run the full harness |
| `make provision` | `ssh -t` + `deploy/provision.sh` (one sudo prompt) |
| `make verify` | `provision.sh --verify`: 10 checks, exit 0 only if all pass |
| `make deploy-daemon` | copy the daemon into `/opt/wopr` and restart the service |
| `make logs` | tail `/opt/wopr/log/wopr.log` |

Both machines run Python 3.14 (Homebrew). `venv/bin/pip install -r requirements.txt` installs
`faster-whisper` and `numpy`; `espeak-ng` comes from Homebrew. Dev runs use port 9093; the
installed service uses 9092.

## Running the harness

```
make local-harness            # or: make harness  (on the mini)
```

Results on the mini, 2026-09-04 (`harness.py run --port 9093`, stub LLM, sidetone −12 dB @ 40 ms
on scenarios):

```
clip/scenario     expect  detected  ttd_ms  result  notes
clean_2600        detect  yes       331     PASS    wink at 831 ms
whistle_drift     detect  yes       332     PASS    wink at 832 ms
whistle_dropout   detect  yes       353     PASS    wink at 853 ms
short_burst       none    no        -       PASS    no wink
speech_only       none    no        -       PASS    no wink
speech_short      none    no        -       PASS    no wink
speech_harmonics  none    no        -       PASS    no wink
sweep             none    no        -       PASS    no wink
far_off           none    no        -       PASS    no wink
hangup            -       yes       1       PASS    1 ms to EOF
bargein           -       yes       31      PASS    cut 31 ms after hold
selftrigger       -       no        -       PASS    1 wink(s) in 20 s with sidetone -12 dB @ 40 ms
whistle_over_tts  -       yes       33      PASS    cut 33 ms after hold
pacing            -       -         21      PASS    p95 21.4 ms, max 22.3 ms over 2778 frames
```

`whistle_drift --repeat 20`: 20/20. `--soak 100`: 100 cycles, worst hangup 1 ms, tasks 3→3,
RSS +0.0 MB.

Useful variants:

```
venv/bin/python harness.py run --port 9093 --only whistle_drift --repeat 20
venv/bin/python harness.py run --port 9094 --only conversation --conversation      # needs STT (+ Ollama unless the daemon runs --stub-llm)
venv/bin/python harness.py play clips/clean_2600.wav --then clips/speech_short.wav --then-after-silence --port 9093 --save-return /tmp/ret.wav
venv/bin/python harness.py play /tmp/handset.wav --port 9093                       # replay a real handset recording (dialplan ext 2603 records one)
```

## Provisioning

Once, from the workstation (prompts for the mini's sudo password):

```
make provision
```

`deploy/provision.sh` is idempotent. Steps: preflight → Homebrew packages (+ `ollama` upgrade) →
`/opt/wopr`, `/opt/asterisk` → **Asterisk 22 source build** (bundled pjproject, `app_audiosocket`
+ `res_audiosocket` + `func_uuid` + `chan_pjsip` + `codec_ulaw`, `BUILD_NATIVE` off) → Asterisk
configs → venv + daemon → models (`ollama pull`, Whisper cache under `/opt/wopr/hf`) → three
LaunchDaemons → `pmset` never-sleep, firewall rule, newsyslog → `--verify`.

The HT802 SIP password is generated once and **printed exactly once**; it lives in
`/opt/asterisk/etc/asterisk/pjsip_secret.conf` (0600). Enter it into the ATA following
`deploy/ht802-settings.md`, then dial 2601 (echo), 2602 (tone), and 2600 (WOPR). Set the ATA's
Offhook Auto-Dial to 2600 if lifting the handset should go straight to WOPR.

Docker is deliberately not used on the mini (Docker Desktop needs a login session; Ollama in a
VM loses Metal). If the native Asterisk build ever fails, `specs/…/research.md` R7/R21 describe
the colima fallback.

## Tuning

Measured on the mini (M4, 16 GB), 2026-09-04, model already resident:

| Stage | Typical |
|---|---|
| endpoint (silence after speech) | 800 ms (`ENDPOINT_MS`) |
| STT, 3.5 s utterance, `base.en` int8 | 300 ms |
| LLM, `qwen3:8b` think off, ~32 tokens | 1.7–1.9 s (prompt 60–180 ms, generation 1.5–1.6 s) |
| TTS, espeak-ng | 50 ms |
| reply audio starts after caller stops | **≈ 2.9 s** (real payphone 2026-09-08: 1.3–2.9 s per turn) |

Knobs, all at the top of `wopr_daemon.py` (printed as `event=config` at startup):

- `RATIO_THRESHOLD`, `MIN_BIN_ENERGY`, `PURITY_MIN`, `HOLD_WINDOWS`, `MISS_BUDGET` — detector.
  Watch `--log-level DEBUG` `event=window` lines while replaying a real handset recording.
- `ECHO_GUARD_WINDOWS` — raise if TTS ever self-triggers on the real handset; lower if a real
  whistle over TTS is missed. 20 (200 ms) passed with a −12 dB / 40 ms simulated sidetone.
- `VAD_ABS_MIN`, `VAD_START_RATIO` — if the payphone line is noisy, raise; if quiet speakers are
  cut off, lower.
- `ESPEAK_SPEED/PITCH/AMPLITUDE` — the voice. `+croak` is the point; do not swap in a neural voice.
- `LLM_NUM_PREDICT`, `MAX_REPLY_CHARS` — reply length. `WOPR_MODEL=llama3.1:8b` in `wopr.env` is the
  documented alternative (`ollama pull` it first).

## Operations

```
sudo launchctl print system/com.crunchwhistle.wopr | grep state     # running?
sudo launchctl kickstart -k system/com.crunchwhistle.wopr           # restart
tail -f /opt/wopr/log/wopr.log                                       # one event per line, call=<uuid8>
sudo /opt/asterisk/sbin/asterisk -rvvv                               # Asterisk console
sudo /opt/asterisk/sbin/asterisk -rx "pjsip show contacts"          # ATA registered?
```

Latency per turn is in the log: sum `utterance ms=`, `stt ms=`, `llm ms=`, `tts ms=` up to
`play_start`; `turn ms=` is the total. Log rotation: `/etc/newsyslog.d/wopr.conf`.

The mini's idle sleep was 1 minute out of the box (it slept mid-harness once); provisioning sets
`pmset -a sleep 0 disksleep 0 autorestart 1`. Dev runs wrap the daemon in `caffeinate -i -s`.

## Not done / follow-ups

- Sentence-streamed TTS (start speaking on the first sentence from the LLM) — would cut ~1 s.
- Optional Docker dev-box rig for exercising the real dialplan without the mini (research R21).
- A dedicated `_wopr` service user instead of the admin account (hardening).
- The 7-day ATA registration soak (SC-014) started 2026-09-08; the unattended-reboot and
  offline checks (quickstart 6.5, 6.6) are still to run.

## First real call (2026-09-08)

HT802 at 10.17.13.139 registered with 3 ms RTT. Off-hook auto-dial to 2600. A human whistle
through the payphone handset scored ratio 7.3 / purity 0.40 (well inside the gates), the wink
and greeting played, and a six-turn conversation followed. Highlights from the log:

    stt text="Start my nuclear war. Can we play a game?"
    llm reply="PROFESSOR FALKEN, YOU WISH TO PLAY A GAME? CHOOSE: CHESS, CHECKERS, BACKG…"
    stt text="with global nuclear war."
    llm reply="PROFESSOR FALKEN, YOU HAVE SELECTED GLOBAL THERMONUCLEAR WAR. I SHALL INI…"

Note the HT802's current firmware puts the profile settings under the FXS PORT1 tabs (General /
SIP / Codec / Call Settings) rather than a separate Profiles page; `deploy/ht802-settings.md`
lists the same values.
