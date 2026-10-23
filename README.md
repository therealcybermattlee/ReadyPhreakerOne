# ReadyPhreakerOne — a phreaking payphone full of movie AIs

A phone-phreaking homage. Lift the handset of a coin-op payphone and a greeter welcomes you:
**"HELLO HACKER. WELCOME TO <THE VENUE>. HOW MAY I HELP YOU?"** (the venue is `WOPR_VENUE`) Say a passphrase — or whistle 2600 Hz —
and a character from the movies answers: WOPR from *WarGames*, Skynet, RoboCop, E.T., Tony Hawk, or
GLaDOS. Each is a live local-LLM conversation in its own voice.

Everything runs on one headless Apple Silicon Mac mini: Asterisk (built from source), the
AudioSocket daemon, local speech recognition, a retro DECtalk-style synthesized voice, and a local
LLM. Nothing in the call path leaves the machine.

## Architecture

```
payphone ──FXS── Grandstream HT802 ──SIP/RTP (μ-law)── Asterisk ──AudioSocket (slin 8 kHz)── wopr_daemon.py
                                                                                              ├─ Goertzel 2600 Hz detector (10 ms windows)
                                                                                              ├─ 480 Hz wink
                                                                                              ├─ energy VAD → faster-whisper (base.en, int8)
                                                                                              ├─ Ollama /api/chat (qwen3:8b, think off)
                                                                                              └─ espeak-ng +klatt → 8 kHz, 20 ms-paced playback
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
| `make sync` | rsync the tree to `~/ReadyPhreakerOne` on the mini |
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
- WOPR's voice — `en-us+klatt` (DECtalk-style; clearer over a phone handset than the old `+croak`).
  Retune live without a redeploy via `WOPR_VOICE` / `WOPR_SPEED` / `WOPR_PITCH` / `WOPR_AMPLITUDE`
  in `wopr.env` (defaults `en-us+klatt` / 135 / 40 / 170). Keep it a retro synth; do not swap in a
  neural voice. The flag reveal keeps its own brighter Johnny 5 voice (`WOPR_FLAG_VOICE`).
- `LLM_NUM_PREDICT`, `MAX_REPLY_CHARS` — reply length. `WOPR_MODEL=llama3.1:8b` in `wopr.env` is the
  documented alternative (`ollama pull` it first).

## Movie-quest flag (easter egg)

WOPR gives up a secret **flag** if the caller re-enacts WarGames over the course of a call. Say
**Joshua** to reach WOPR, then three beats, in any order, tracked in code (not by the LLM, so the
secret never leaks or fires early):

1. ask to **play a game**
2. choose **global thermonuclear war**
3. deliver the lesson — **"the only winning move is not to play"**

On the third, WOPR plays a fanfare and speaks the flag (default `JOSHUA FIVE BY FIVE`). The beat
regexes are tolerant of local-STT mishears (e.g. "Joshua" → "Noshua"); Joshua and the winning-move
line are the real gates. Every beat logs `event=quest beat=… progress=n/3`, and the reveal logs
`event=flag_unlocked flag=…`.

Customize in `/opt/wopr/wopr.env`:

```
WOPR_FLAG=YOUR-CTF-TOKEN-HERE          # what WOPR speaks/logs on completion
WOPR_FLAG_TEXT=... {flag} ...          # the reveal line ({flag} is substituted)
WOPR_NO_FLAG=1                         # disable the quest entirely
```

A second whistle (barge-in) resets the quest along with the conversation.

### "How about a nice game of chess?"

Straight after the flag, WOPR offers a game of chess (as it does in the film). If the caller says
**yes**, WOPR narrates a real game **one move at a time**, caller-paced: any reply ("next", "go",
"move"…) advances a ply; "stop" / "quit" / "resign" adjourns it. The default game is the **Opera
Game** (Morphy, Paris 1858) — a queen sacrifice into mate on move 17. Saying "chess" at any point
also starts a game, so it's replayable without the flag. Turns are logged with `kind=chess`.

```
WOPR_NO_CHESS=1                        # disable the chess game
WOPR_CHESS="e4 e5 Nf3 ..."            # override the SAN move list (must end in # to declare mate)
WOPR_CHESS_OFFER=...                   # the "nice game of chess?" offer line
WOPR_CHESS_INTRO=...                   # spoken before the first move
```

No chess engine: the moves are canned SAN, converted to speech (`Rd8#` → "ROOK TO D 8, CHECKMATE")
and only spoken, never validated.

## Recording & transcript dataset

Every call is recorded and every turn is logged to a dataset, with no announcement to the caller
(operator's decision; the operator is a party to the call).

- **Audio**: Asterisk `MixMonitor` on extensions 2600/2605/2606 writes one mixed-mono 8 kHz WAV
  per call to `/opt/wopr/recordings/<YYYYmmdd-HHMMSS>-<uniqueid>[-cb|-win].wav` (~1 MB/min).
- **Transcripts**: set `WOPR_TRANSCRIPT=/opt/wopr/transcripts/dataset.jsonl`; the daemon appends
  one JSON line per turn: `{ts, call, callback, turn, kind, user, wopr, quest, unlocked, model}`
  where `kind` is reply / joshua / flag. Tiny (well under 100 MB for a large event).

Sizing for a 2-day, few-hundred-call event: roughly 1–3 GB of audio plus negligible text. The
mini has ~265 GB free, so no external drive is needed; copy the folders off afterward for backup.

## Analysis engine (the human experiment)

The daemon writes a per-call metrics record to `WOPR_SESSIONS` (JSONL): whistle time, points
(quest beats), first-point timing, win time, turns, callback kind. `analyze.py` aggregates the
metrics + transcripts into a report and an HTML page with a word cloud of what callers said:

```
python analyze.py --sessions /opt/wopr/transcripts/sessions.jsonl \
                  --transcript /opt/wopr/transcripts/dataset.jsonl --html report.html
```

Reports: call funnel (whistled → each beat → won), points-per-call distribution, and timing
stats for "music heard before whistle", "whistle → first point", "call start → win", duration,
and turns. The HTML adds a WOPR-green word cloud of caller speech. Stdlib only.

## WOPR calls back

If the caller chooses **global thermonuclear war** and then hangs up, WOPR rings the payphone
back after a short pause (`CALLBACK_DELAY_S`, default 20 s) — it doesn't like being left mid-game.
The daemon asks Asterisk to originate a call to the payphone's callback extension (2605), which
carries a sentinel UUID so the daemon knows to drop the answerer straight into WOPR, no whistle
needed. A cooldown (`CALLBACK_COOLDOWN_S`, default 90 s) prevents runaway loops, and a call that
is itself a callback never schedules another.

If the caller hung up mid-quest (chose thermonuclear war but hadn't finished the flag), the callback **resumes their progress** — they can complete the flag by delivering the final line when WOPR rings back, instead of starting over.

Requires the `2605` dialplan extension (in `deploy/asterisk/extensions.conf`). Disable with
`WOPR_NO_CALLBACK=1`; point it elsewhere with `WOPR_CALLBACK_DIAL` / `WOPR_ASTERISK_BIN`.
Log events: `callback_scheduled`, `callback_dialing`, `callback_result`, `callback_answered`.

## Attract loop (waiting audio)

While the line is armed and waiting for the whistle, the daemon can loop an audio file to the
caller instead of silence — a song, or a spoken hint. The instant a 2600 Hz whistle is detected,
it cuts to the wink and WOPR. Detection runs on the caller's mic and the sidetone guard already
prevents the outbound audio from self-triggering, so listening-while-playing is safe.

```
# make a spoken default (Aperture-style "whistle to begin") with the local voice:
venv/bin/python harness.py attract --out clips/attract.wav
# or convert your own song/message to the required 8 kHz mono 16-bit:
venv/bin/python harness.py attract --from-wav still_alive.wav --out clips/attract.wav
#   (or: ffmpeg -i still_alive.mp3 -ac 1 -ar 8000 -sample_fmt s16 clips/attract.wav)

# enable it: point WOPR_ATTRACT at the file (in /opt/wopr/wopr.env) and restart the service.
```

Set `WOPR_ATTRACT=` empty (or remove it) for silence. `ATTRACT_GAIN` (top of `wopr_daemon.py`,
default 0.6) keeps the loop below full scale so handset sidetone doesn't swamp whistle detection.
Note: "Still Alive" (Aperture Science / Portal) is copyrighted by Valve — supply it yourself.

## Firewall

The mini runs a pf ruleset (`deploy/pf-crunchwhistle.conf`) that default-denies inbound and
opens only what the stack needs: SSH (22), SIP (5060/udp) and RTP (10000–10100/udp) from the
ATA's subnet, and ICMP. Loopback and all outbound stay open, so the daemon ↔ Ollama ↔ Asterisk
paths are untouched. This closes the extra ports Asterisk opens by default (IAX2 4569, etc.).

```
sudo ./deploy/firewall.sh enable    # install + turn on, with a 180 s dead-man
sudo ./deploy/firewall.sh keep      # cancel the dead-man once SSH is confirmed
sudo ./deploy/firewall.sh persist   # boot-time loader (com.crunchwhistle.pf)
sudo ./deploy/firewall.sh status
```

The dead-man auto-disables pf if `enable` locks you out. SIP/RTP are scoped to 10.17.13.0/24;
if the ATA moves subnets, edit the conf and re-run `enable`/`keep`.

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
