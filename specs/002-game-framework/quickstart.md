# Quickstart: Validate the Game Framework

Runnable checks that prove the feature works end to end. Assumes the daemon runs on the mini
(`wopr-mini`) from `/opt/wopr` and the dev checkout is on the workstation. Paths and contracts are in
[data-model.md](./data-model.md) and [contracts/game-definition.md](./contracts/game-definition.md).

## Prerequisites

- Daemon deployed and healthy (`event=listen` in `/opt/wopr/log/wopr.log`), Ollama + Whisper warm.
- A working handset (or the harness) to exercise audio.
- `WOPR_SESSIONS` and `WOPR_STATS_DB` set (already configured) so analytics are written.

## 1. Unit tests (pure logic) — no phone needed

```
# on the dev box
./venv/bin/python -m unittest tests.test_dsp
```

Expected: green, including new tests for
- objective matching tolerance (mishears still match),
- ordered vs any-order quest completion,
- cutscene flow selects clip/message/goodbye and marks `completed`,
- stats: `won` for a quest, `completed` for a cutscene, aggregate `wins`/`completions`.

## 2. WOPR regression (Quest) — the win must be identical

Summon WOPR (whistle 2600 Hz or say "Joshua"), then re-enact the four beats in any order:
Joshua → ask to play a game → global thermonuclear war → "the only winning move is not to play".

Expected: the flag reveal `<WOPR flag>` plays exactly as before (fanfare + flag intro +
Johnny-5 reveal voice), the chess offer follows, and the call logs `flag_unlocked` and
`event=call_metrics … won=1`. No behavioral difference from the pre-framework build.

## 3. A newly-defined Quest game reaches a win

Summon a quest game (e.g. RoboCop via "RoboCop"). Satisfy its objectives (recite the three prime
directives). 

Expected: the reveal line + its flag are spoken; `sessions.jsonl` shows `games.robocop.won=true`;
`game_stats.json` increments `games.robocop.wins`.

## 4. Cutscene plays and ends the call

Summon GLaDOS ("the cake is a lie") and Tony Hawk ("kickflip").

Expected:
- **GLaDOS**: song plays → goodbye → the payphone hangs up (line returns to on-hook); no prompt for
  input at any point.
- **Tony Hawk**: kickflip clip → one pep-talk message (generated, or the fixed fallback) → goodbye →
  hang up.
- In both, the log shows the game reached, then a normal `call_end` with the recording finalized and
  `sessions.jsonl` / `game_stats.json` updated with `completed`/`completions`.

Verify the hang-up and single teardown:

```
ssh wopr-mini 'grep -aE "event=experience|event=call_end|call_metrics" /opt/wopr/log/wopr.log | tail -6'
```

Expected: one `call_end` per cutscene call and one metrics line (no double-write).

## 5. Caller hangs up mid-cutscene

Start a cutscene and hang up before it finishes.

Expected: clean teardown once; the game recorded as reached but **not** completed; no traceback in
the log.

## 6. Analytics distinguish wins vs completions

```
ssh wopr-mini '/opt/wopr/venv/bin/python /opt/wopr/analyze.py'
```

Expected: the PER-GAME BREAKDOWN shows quest games with **wins** and cutscene games with
**completions**, computed without error over a dataset mixing new and legacy records.

## 7. Offline check

With the phone island offline (no internet), repeat steps 2–4.

Expected: everything works — STT, LLM, TTS, recording, stats — with no external network access.
