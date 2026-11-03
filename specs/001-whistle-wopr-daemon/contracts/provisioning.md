# Contract: provisioning the Mac mini (`deploy/`)

Everything the daemon needs at call time lives on the mini. This contract defines the operator
interface for getting it there and checking it is there. All commands run over SSH as an admin
user with sudo.

## `deploy/provision.sh`

```
./deploy/provision.sh [--preflight] [--verify] [--skip-asterisk] [--skip-models] [--asterisk-version 22]
```

Run over `ssh -t wopr-mini` as `mattlee`; the script calls `sudo -v` up front (password prompt
once) and uses `sudo` only for `/Library/LaunchDaemons`, `pmset`, `socketfilterfw`, and
`/etc/newsyslog.d`. `--preflight` prints the same facts as the 2026-09-04 probe and exits.

- Idempotent and re-runnable (FR-040). Every step checks its own postcondition before acting and
  prints `SKIP <step>` / `DONE <step>`.
- `--verify` performs no changes; runs the checks in the table below and exits 0 only if all pass.
- Exit 0 success; 1 a step failed (message names the step); 2 preconditions unmet (not Apple
  Silicon, no Homebrew, no Xcode CLT, < 16 GB RAM warning is non-fatal but switches the default
  model to `llama3.2:3b`).
- Secrets: the HT802 SIP password is generated once into `/opt/asterisk/etc/asterisk/pjsip_secret.conf`
  (0600, `#include`d) and printed exactly once with instructions to enter it into the ATA.

### Steps and postconditions

| # | Step | Postcondition checked by `--verify` |
|---|---|---|
| 1 | Preflight | `uname -m` = arm64; `brew --prefix` works; `xcode-select -p` works; RAM reported |
| 2 | Homebrew packages | `espeak-ng jansson libxml2 pkg-config libedit` installed; `ollama` ≥ 0.33; `python@3.14 sqlite openssl@3` present |
| 3 | Directories | `/opt/wopr`, `/opt/asterisk`, `/opt/wopr/log` exist, owned by `mattlee`; no stray hand-started `ollama serve` on 11434 |
| 4 | Asterisk build | `/opt/asterisk/sbin/asterisk -V` reports the requested major; `module show like audiosocket` lists 2 modules Running; `func_uuid`, `chan_pjsip`, `codec_ulaw` loaded |
| 5 | Asterisk config | files from `deploy/asterisk/` installed; `pjsip show endpoints` lists `payphone`; `dialplan show payphone` lists 2600/2601/2602 |
| 6 | Python venv + daemon | `/opt/wopr/venv/bin/python -c 'import faster_whisper, numpy'` ok; `/opt/wopr/wopr_daemon.py --check` exits 0 |
| 7 | Models | `ollama list` includes `qwen3:8b` (already true); Whisper `base.en` present under `/opt/wopr/hf` |
| 8 | LaunchDaemons | three plists in `/Library/LaunchDaemons`, `launchctl print system/<label>` shows state = running for each |
| 9 | Host settings | `pmset -g` sleep 0; firewall allows asterisk if firewall enabled; `/etc/newsyslog.d/wopr.conf` present |
| 10 | End-to-end | `harness.py run --port 9092 --only clean_2600,hangup` passes against the installed service |

## `deploy/launchd/*.plist`

Common keys: `RunAtLoad`, `KeepAlive`, `UserName=mattlee`, `GroupName=staff`,
`WorkingDirectory`, `EnvironmentVariables` (always including
`PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`), `StandardOutPath`/`StandardErrorPath`
under `/opt/wopr/log/`, `ThrottleInterval=5`.

| Label | Program | Notes |
|---|---|---|
| `com.crunchwhistle.ollama` | `/opt/homebrew/bin/ollama serve` | `OLLAMA_HOST=127.0.0.1:11434`, `OLLAMA_MODELS=/Users/<user>/.ollama/models`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_NUM_PARALLEL=1` |
| `com.crunchwhistle.asterisk` | `/opt/asterisk/sbin/asterisk -f -U mattlee -G staff -C /opt/asterisk/etc/asterisk/asterisk.conf` | `ProcessType=Interactive` |
| `com.crunchwhistle.wopr` | `/opt/wopr/venv/bin/python /opt/wopr/wopr_daemon.py` | env from `/opt/wopr/wopr.env`; `ProcessType=Interactive` |

Operator commands (documented in README): `sudo launchctl kickstart -k system/com.crunchwhistle.wopr`
(restart), `sudo launchctl print system/com.crunchwhistle.wopr` (state), `tail -f /opt/wopr/log/wopr.log`.

## `deploy/asterisk/`

| File | Contract |
|---|---|
| `menuselect.makeopts` | Enables `app_audiosocket res_audiosocket func_uuid chan_pjsip codec_ulaw`; disables `BUILD_NATIVE` |
| `asterisk.conf` | All dirs under `/opt/asterisk`; `runuser=mattlee`, `rungroup=staff` |
| `modules.conf` | `require=` the four modules above; `noload` list for unused subsystems |
| `pjsip.conf` | transport udp 5060; endpoint/auth/aor `payphone`, ulaw only, `qualify_frequency=60`; `#include pjsip_secret.conf` |
| `extensions.conf` | `[payphone]`: `2600` → Answer, `Set(WOPR_UUID=${UUID()})`, `AudioSocket(${WOPR_UUID},127.0.0.1:9092)`; `2601` Echo; `2602` Milliwatt |
| `rtp.conf` | `rtpstart=10000 rtpend=10100` |

## `deploy/ht802-settings.md`

Screen-by-screen HT802 settings for FXS Port 1 (see research.md R18) plus verification commands
(`pjsip show contacts`, `pjsip show endpoint payphone`, `core show channels` during a call showing
`ulaw`). This is documentation, not automation (spec assumption).

## `Makefile` targets (run from the dev box)

| Target | Does |
|---|---|
| `sync` | rsync tree to `$MINI:~/CrunchWhistle/` |
| `probe` | `ssh $MINI` read-only hardware/software facts (same as `provision.sh --preflight`) |
| `test` | `ssh $MINI 'cd ~/CrunchWhistle && venv/bin/python -m unittest discover -s tests'` |
| `harness` | start dev daemon on 9093 with `--stub-llm`, run `harness.py run --port 9093`, stop it |
| `provision` | `sync` then `ssh -t $MINI '~/CrunchWhistle/deploy/provision.sh'` (interactive sudo prompt) |
| `verify` | `ssh $MINI '~/CrunchWhistle/deploy/provision.sh --verify'` |
| `deploy-daemon` | copy `wopr_daemon.py` + `requirements.txt` to `/opt/wopr`, restart the wopr service |
| `logs` | `ssh $MINI 'tail -f /opt/wopr/log/wopr.log'` |

`MINI` is read from `.env.local` (gitignored; currently `MINI=wopr-mini`). Every remote command
is wrapped as `ssh $MINI 'export PATH=/opt/homebrew/bin:$PATH; …'` because non-interactive SSH
on the mini lacks Homebrew on PATH. Every target fails fast with a clear message if `MINI` is unset.
