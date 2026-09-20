# Phase 1 Data Model: Conversational Operator

In-process value objects on the daemon's existing dataclasses. No new storage.

## DirectoryEntry (new, attached to `Experience`)

| Field | Type | Notes |
|---|---|---|
| `name` | str | display name the Operator may say (e.g. `WOPR`, `RoboCop`) |
| `say` | tuple[str, ...] | spoken names a caller can use (rendered as "say Joshua"); a subset of the wake regex's intent |
| `blurb` | str | one short line she may use to describe it (e.g. "a war computer that wants to play a game") |

## Experience (extended)

| Field | Type | Notes |
|---|---|---|
| `listed` | bool | default `True`; `False` = **easter egg**: reachable by wake phrase, never in the directory |
| `directory` | DirectoryEntry \| None | required when `listed` and the experience is a game; utilities carry `None` |

**Validation** (startup, `validate_games`): a listed game without a `directory` entry is logged
`event=game_invalid why="no directory entry"` (the game still runs; the Operator just cannot
describe it). An easter egg must not have `directory` set (logged, ignored).

**Archetype rule** unchanged. The Operator itself: key `operator`, `system_prompt` rendered at
startup, `listed=False`, not a game (excluded from `GAME_KEYS` like greeter/goodbye).

## Operator content (module-level, author-editable)

| Item | Type | Notes |
|---|---|---|
| `OPERATOR_PERSONA` | str | the switchboard persona rules (caps, terse, never break character, brush-off rule) |
| `OPERATOR_CLUES` | tuple[str, ...] | ordered secret-word clues; ≥ 3 |
| `OPERATOR_CLUES_EXHAUSTED` | str | line after the last clue |
| `OPERATOR_NUDGES` | tuple[str, ...] | rotating silence nudges |
| `OPERATOR_FALLBACK` | str | fixed line when a reply fails, times out, or is emptied by the filter |
| `OPERATOR_BRUSHOFF` | str | replacement for a reply that violated the secret-word rule |
| `SECRET_WORD_ASK_RE` | pattern | intercepts clue requests |
| `LIST_OPTIONS_RE` | pattern | intercepts "list the options" → recorded hints clip |

`render_directory() -> str` produces the directory block inserted into the system prompt; it is
computed once at startup from `WAKE_EXPERIENCES` (listed games only).

## CallSession (extended, per call, transient)

| Field | Type | Notes |
|---|---|---|
| `clue_idx` | int | next clue to give; reset per call, never persisted |
| `nudge_idx` | int | rotates through `OPERATOR_NUDGES` |

`idle_nudge_s` is a property of the active `Experience` (Operator: 12.0; games: `None`).

## Config (extended)

| Field | Type | Notes |
|---|---|---|
| `enable_operator` | bool | `--no-operator` / `WOPR_OPERATOR=0` forces the recorded menu loop |
| `operator_voice/speed/pitch` | str/int/int | espeak voice for the Operator |
| `operator_reply_budget_s` | float | default 5.0; slower replies fall back to `OPERATOR_FALLBACK` |

## Reply pipeline (state transitions for one Operator turn)

```
utterance ─STT─▶ text
  ├─ MENU_RETURN / name of another experience ─▶ existing switch (unchanged)
  ├─ SECRET_WORD_ASK_RE ─▶ speak OPERATOR_CLUES[clue_idx]; clue_idx += 1        (no LLM)
  ├─ LIST_OPTIONS_RE ─▶ play OPERATOR_FRAMES (recorded hints)                    (no LLM)
  └─ else ─▶ llm_reply (budgeted) ─▶ postprocess_reply ─▶ operator_filter ─▶ TTS ─▶ play
                 │ timeout/error                              │ emptied / violation
                 └──────────▶ OPERATOR_FALLBACK ◀─────────────┘ (or OPERATOR_BRUSHOFF)
capture timeout (idle_nudge_s) ─▶ speak OPERATOR_NUDGES[nudge_idx]; nudge_idx += 1; capture again
```

## Analytics

Unchanged schema. The Operator's `note_game` calls are skipped (she is not in `GAME_KEYS`), so
`games_played`, `primary_game`, `reached_chat`, and the aggregate DB do not count her turns.
`sessions.jsonl` gains nothing; `dataset.jsonl` turn records keep `kind` = `reply` / `clue` /
`nudge` / `hints` for the Operator's turns so the transcript stays complete.
