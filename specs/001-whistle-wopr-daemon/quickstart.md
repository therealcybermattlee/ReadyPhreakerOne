# Quickstart: validating the 2600 Hz → WOPR daemon on the Mac mini (rev 2)

Contracts: [audiosocket-protocol](contracts/audiosocket-protocol.md) ·
[harness-cli](contracts/harness-cli.md) · [daemon-config](contracts/daemon-config.md) ·
[provisioning](contracts/provisioning.md). Entities: [data-model.md](data-model.md).

## Prerequisites

- Dev box: Python 3.14 with numpy, `rsync`, key auth to the mini via the `wopr-mini` SSH alias.
  `.env.local` already contains `MINI=wopr-mini`.
- Mini (verified 2026-09-04): Apple M4, 16 GB, macOS 26.6.2, Homebrew 5.1.6 at `/opt/homebrew`,
  `python@3.14`, Xcode CLT, Ollama 0.17.0 with `qwen3:8b` pulled, admin user `mattlee` with
  password sudo, internet during provisioning only.
- HT802 on the same LAN, web UI reachable from the mini.

## Scenario 0 — Hardware and OS facts (read-only; done 2026-09-04, re-run to catch drift)

```bash
make probe      # or: ssh wopr-mini 'export PATH=/opt/homebrew/bin:$PATH; uname -m; sw_vers; sysctl -n hw.memsize; brew --version; python3.14 --version; ollama list; which asterisk espeak-ng'
```

Expected (matches plan.md "Verified facts"): `arm64`, 16 GB, macOS 26.6.2, Homebrew 5.1.6,
Python 3.14, `qwen3:8b` listed, asterisk and espeak-ng absent until provisioning. Any deviation
updates plan.md's Technical Context before proceeding.

## Scenario 1 — Unit tests (dev box or mini, no services)

```bash
python -m unittest discover -s tests -v
```

Expected: all pass. Framing round-trip, Goertzel ratios (2600 vs 2500/2700/silence/floor),
hold/miss-budget/re-arm logic, resampler, WAV synthesis shapes, Ollama response parsing.

## Scenario 2 — Detection and false-positive suite (P1, hermetic, dev box or mini)

Terminal A: `python wopr_daemon.py --stub-llm --port 9093`
Terminal B: `python harness.py synth && python harness.py run --port 9093 --json results.json`

| Check | Pass condition |
|---|---|
| `clean_2600`, `whistle_drift`, `whistle_dropout` | detected, ttd_ms < 500 |
| `short_burst`, `speech_only`, `speech_harmonics`, `sweep`, `far_off` | not detected |
| `bargein` | silence ≤ 100 ms after second detect; second wink ≤ 1 s |
| `hangup` | EOF ≤ 1000 ms |
| `pacing` | p95 inter-frame ≤ 30 ms; max gap ≤ 100 ms |
| exit code | 0 |

Daemon log shows `event=detect` per detect clip, one `bargein`, a `call_end` per connection, no
tracebacks. Rate check for SC-002: `harness.py run --port 9093 --only whistle_drift --repeat 20` → ≥ 19/20.

On the dev box this needs only numpy and espeak-ng (`brew install espeak-ng`); on 3.14 the STT
import is skipped under `--stub-llm` (lazy import).

## Scenario 3 — Tuning with DEBUG ratios

`python wopr_daemon.py --stub-llm --port 9093 --log-level DEBUG 2>&1 | grep event=window` while
running `harness.py run --port 9093 --only speech_harmonics`. Expected: `pass=0` almost
everywhere, no streak ≥ 10. Set `MIN_BIN_ENERGY` / `VAD_ABS_MIN` from these numbers.

## Scenario 4 — Provision the mini

```bash
make provision          # sync + sudo deploy/provision.sh on the mini (20–40 min incl. Asterisk build and model pulls)
make verify             # all 10 checks PASS, exit 0
```

Expected `--verify` output ends with `10/10 checks passed`. The Asterisk build step is the
riskiest; if it fails, the failure names the configure/make stage and research.md R7 gives the
colima fallback.

Then, still on the mini:

```bash
ssh $MINI 'cd ~/CrunchWhistle && python3 harness.py run --port 9092'
```

against the *installed* service with Ollama live (no `--stub-llm`): same table as Scenario 2,
and the log now contains `llm_warm ms=` at startup. Note `make provision` prompts once for the
sudo password over `ssh -t`.

## Scenario 5 — Full conversation, stub then live LLM (on the mini)

Stub: `ssh $MINI 'cd ~/CrunchWhistle && python3 harness.py play clips/clean_2600.wav --then clips/speech_only.wav --port 9093'`
with a dev daemon on 9093 `--stub-llm`. Expected log order: `detect` → `play_start` → `play_end`
→ `capture_start` → `utterance ms=` → `stt text="…" ms=` → `llm ms= (stub)` → `tts ms=` → `play_start`.

Live: same against port 9092 (installed service). Expected `llm ms=<n> prompt_ms= gen_ms= tokens=`
with total utterance→play_start ≤ 5000 ms once warm; reply is uppercase, terse, names Professor
Falken, proposes a game. Then `sudo launchctl kill SIGTERM system/com.crunchwhistle.ollama` and
repeat: expected `pipeline_error stage=llm` at WARNING, canned line plays, call continues;
Ollama is restarted by launchd within seconds.

## Scenario 6 — Phone switch and ATA (User Story 5)

1. Enter the settings from `deploy/ht802-settings.md` into the HT802 (password from provisioning output).
2. `ssh $MINI 'sudo /opt/asterisk/sbin/asterisk -rx "pjsip show contacts"'` → `payphone` Avail.
3. Lift the handset, dial `2601`: you hear your own voice echoed. Dial `2602`: steady 1 kHz tone.
   `asterisk -rx "core show channels verbose"` during the call shows `ulaw`.
4. Dial `2600` (or lift the handset if auto-dial is set): silence. Whistle 2600 Hz → wink →
   "GREETINGS PROFESSOR FALKEN." Talk, pause, reply within ~5 s. Whistle over the reply → cut,
   wink, fresh greeting. Hang up → `call_end` in `/opt/wopr/log/wopr.log` within 1 s.
5. Reboot test (SC-012): `ssh $MINI 'sudo reboot'`, do **not** log in, wait 3 min, lift the
   handset and repeat step 4.
6. Offline test (SC-013): unplug the mini's uplink from the internet (keep the LAN switch),
   repeat step 4.
7. Registration soak (SC-014): after 7 days, `pjsip show contacts` still shows Avail; `pjsip show
   endpoint payphone` shows continuous qualify.

If step 4's whistle over TTS self-triggers without a whistle, raise `ECHO_GUARD_WINDOWS`; if a
real whistle over TTS fails, lower it. If real whistles miss, record the handset via a scratch
`Record()` extension and replay with `harness.py play` while tuning with Scenario 3.


## Validation log

| Date | Scenario | Result |
|---|---|---|
| 2026-09-04 | 0 hardware probe | M4, 16 GB, macOS 26.6.2, Homebrew, python@3.14, Ollama 0.17 + qwen3:8b present |
| 2026-09-04 | 1 unit tests | 30/30 on dev box and mini |
| 2026-09-04 | 2 harness suite | 14/14 on mini (3/3 detects, 0/6 false positives, bargein 31 ms, pacing p95 21 ms); whistle_drift 20/20; soak 100 cycles clean |
| 2026-09-04 | 3 DEBUG ratios | MIN_BIN_ENERGY set to 1e8 from corpus measurements |
| 2026-09-04 | 5 live LLM | reply 1.9–2.1 s after speech end (qwen3:8b think off) |
| 2026-09-08 | 4 provision | 10/10 after two fixes (pjproject dir, modules.conf pubsub dependency) |
| 2026-09-08 | 6.1–6.4 real phone | HT802 registered (RTT 3 ms); off-hook auto-dial to 2600; real whistle detected (ratio 7.3, purity 0.40); 6-turn conversation; turn latency 1.3–2.9 s |
| 2026-09-08 | 6.5 reboot | Services + ATA auto-recover on boot, BUT FileVault halts the mini at the unlock screen until the disk password is entered at the console — unattended boot needs FileVault off or a manual unlock |
| pending | 6.6 offline (uplink unplugged) | |
| pending | 6.7 7-day registration soak | started 2026-09-08 |
| 2026-09-08 | firewall | pf default-deny inbound; only 22 + 5060/RTP (from ATA subnet) + ICMP; ATA stays registered through it; boot loader installed |
