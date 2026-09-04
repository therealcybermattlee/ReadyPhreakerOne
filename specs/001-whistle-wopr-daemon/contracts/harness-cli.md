# Contract: `harness.py` command-line interface

Phone-free test harness. Runs as an AudioSocket **client** against a live daemon. Stdlib +
numpy; also shells out to `espeak-ng` for speech clips.

## Subcommands

### `python harness.py synth [--out clips/] [--seed N]`

Generates the clip corpus (see research.md R13) as 8 kHz mono int16 WAVs plus
`clips/manifest.json`:

```json
[
  {"name": "clean_2600", "file": "clean_2600.wav", "expect_detect": true,  "onset_ms": 500, "max_ttd_ms": 500},
  {"name": "speech_only", "file": "speech_only.wav", "expect_detect": false, "onset_ms": null, "max_ttd_ms": null}
]
```

Deterministic for a given `--seed`. Exit 0 on success; exit 2 if `espeak-ng` is missing (speech
clips are skipped with a warning and the manifest omits them).

### `python harness.py run [--host 127.0.0.1] [--port 9092] [--clips clips/] [--only NAME,...] [--repeat N] [--json PATH] [--timeout-s 10]`

For each manifest entry (× `--repeat`): connect, send UUID frame, stream the clip at 20 ms pace,
keep reading return audio for `--timeout-s` after the clip ends (or until wink seen, for
detect clips), then send TERMINATE and close.

Detection signal: Goertzel at 480 Hz on return audio (N = 100, ≥ 5 consecutive passing windows).

Then runs scenarios (unless `--only` excludes them):

- `bargein`: stream `clean_2600`; wait for wink; wait until return audio is non-silent for ≥ 200 ms
  (greeting playing); stream `clean_2600` again while continuing to read; PASS if return audio is
  silent within 100 ms of the second wink-detect *and* a second wink follows within 1 s.
- `hangup`: stream `clean_2600`; wait for wink; send TERMINATE; PASS if the daemon closes the
  socket (EOF) within 1000 ms.
- `pacing`: during any run, record inter-frame arrival intervals of return audio; PASS if the
  95th percentile is ≤ 30 ms and no gap exceeds 100 ms.

**Output** (stdout):

```
clip               expect   detected  ttd_ms   result
clean_2600         detect   yes        312     PASS
whistle_drift      detect   yes        338     PASS
speech_only        none     no         -       PASS
...
bargein            -        -          62 ms to silence   PASS
hangup             -        -          41 ms to EOF       PASS
pacing             -        -          p95 21 ms          PASS
Summary: 11 passed, 0 failed  (detect rate 3/3, false positives 0/5)
```

`--json PATH` additionally writes the `ClipResult` list (see data-model.md).

**Exit codes**: 0 all passed; 1 one or more mismatches; 2 could not connect / setup error.

### `python harness.py play FILE.wav [--host] [--port]`

Streams an arbitrary 8 kHz WAV (e.g. a real recording of someone whistling) and reports whether
and when the wink was heard. For tuning against real handset captures.

## Invariants

- The harness never imports `faster_whisper`; it depends only on numpy and the stdlib.
- The harness's Goertzel and framing helpers are independently unit-tested in `tests/test_dsp.py`.
- Tunables the daemon exposes as constants are printed at the top of every `run` so results are
  reproducible.
