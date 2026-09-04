# Contract: `wopr_daemon.py` configuration, logging, and service interface (rev 2)

## Invocation

```
python wopr_daemon.py [--bind ADDR] [--port N] [--log-level LEVEL] [--stub-llm]
                      [--ollama-url URL] [--model NAME] [--whisper-model NAME] [--check]
```

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--bind` | `WOPR_BIND` | `127.0.0.1` | Listen address. AudioSocket has no auth; Asterisk is on the same host, so loopback is correct. |
| `--port` | `WOPR_PORT` | `9092` | Listen port. Dev runs use 9093. |
| `--log-level` | `WOPR_LOG_LEVEL` | `INFO` | `DEBUG` adds per-window ratios (~100 lines/s/call). |
| `--stub-llm` | `WOPR_STUB_LLM=1` | off | Replace the Ollama call with canned WOPR lines; skip warm-up. No Ollama needed. |
| `--ollama-url` | `WOPR_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama base URL. Must be loopback in production (FR-034). |
| `--model` | `WOPR_MODEL` | `qwen3:8b` | Ollama model tag. Must already be pulled (it is, on the mini). |
| `--whisper-model` | `WOPR_WHISPER_MODEL` | `base.en` | faster-whisper model name. Must already be cached (`HF_HUB_OFFLINE=1` in service env). |
| `--check` | — | — | Verify espeak-ng (+croak), load Whisper, GET `/api/tags` and confirm the model is present (unless stub), print tunables, exit 0/1. Does not listen. |

CLI flag beats env var beats default. There are no secrets.

## Tunables (constants at the top of the file, not flags)

Printed at startup at INFO as one `event=config` line.

```
GOERTZEL_N=80  BINS_HZ=(2500,2600,2700)  RATIO_THRESHOLD=0.35  MIN_BIN_ENERGY=…
HOLD_WINDOWS=30  MISS_BUDGET=2  REARM_WINDOWS=50  ECHO_GUARD_WINDOWS=20
WINK_HZ=480  WINK_MS=200  WINK_AMPLITUDE=0.5
VAD_START_RATIO=3.0  VAD_ABS_MIN=…  VAD_START_WINDOWS=3  ENDPOINT_MS=800
MIN_UTTERANCE_MS=300  MAX_UTTERANCE_MS=15000
ESPEAK_VOICE="en-us+croak"  ESPEAK_SPEED=140  ESPEAK_PITCH=30  ESPEAK_AMPLITUDE=150
LLM_NUM_PREDICT=80  LLM_NUM_CTX=2048  LLM_TEMPERATURE=0.7  LLM_TIMEOUT_S=20
LLM_WARMUP_RETRY_S=2  HISTORY_MAX_TURNS=20  MAX_REPLY_CHARS=280
OUTBOUND_QUEUE_FRAMES=1500
```

## Ollama request/response contract used by the daemon

`POST {ollama-url}/api/chat`, JSON:

```json
{"model": "<WOPR_MODEL>", "stream": false, "think": false, "keep_alive": -1,
 "messages": [{"role": "system", "content": "<WOPR persona>"}, ...history, {"role": "user", "content": "<stt text>"}],
 "options": {"num_predict": 80, "num_ctx": 2048, "temperature": 0.7, "stop": ["\n\n"]}}
```

Reads `message.content` (any `<think>…</think>` block stripped), `prompt_eval_duration`,
`eval_duration`, `eval_count`. Any non-200,
timeout (20 s), or unparsable body is a recoverable `pipeline_error stage=llm`. Warm-up at
startup: same call with `num_predict: 1` and a fixed prompt, retried every 2 s until it succeeds.

## Log line format

```
<ts> <LEVEL> call=<uuid8> event=<name> [k=v ...]
```

| Level | Events |
|---|---|
| DEBUG | `window ratio= e26= pass= masked= run=` |
| INFO | `config`, `listen`, `llm_warm ms=`, `call_start`, `detect ratio= run= t_ms=`, `bargein`, `state from= to=`, `capture_start`, `utterance ms=`, `stt text="…" ms=`, `llm ms= prompt_ms= gen_ms= tokens= chars=`, `tts ms= frames=`, `play_start`, `play_end`, `call_end dur_ms= turns=` |
| WARNING | `proto_error code=`, `unknown_frame type=`, `empty_utterance`, `pipeline_error stage= err=`, `llm_warm_retry` |
| ERROR | `bad_handshake`, `startup_check_failed what=` |

Under launchd, stdout/stderr go to `/opt/wopr/log/wopr.log` (rotated by newsyslog).

## Deployment files

- `requirements.txt`: `faster-whisper>=1.1,<2`, `numpy>=1.26,<3`. Nothing else.
- Service plist: `deploy/launchd/com.crunchwhistle.wopr.plist` — see `contracts/provisioning.md`.
- Env file: `/opt/wopr/wopr.env` (from `deploy/wopr.env.example`), sourced by the plist's
  `EnvironmentVariables` at provision time: `WOPR_*` above plus `HF_HUB_OFFLINE=1`,
  `HF_HOME=/opt/wopr/hf`, `PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
