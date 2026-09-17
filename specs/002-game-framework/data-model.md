# Phase 1 Data Model: Game Framework

Entities are in-process value objects (dataclasses) declared in the daemon, plus the per-call and
aggregate records they feed. No database engine; storage is JSONL + a JSON aggregate + WAV files.

## Game (extends the existing `Experience`)

The declarative definition of one playable character.

| Field | Type | Notes |
|---|---|---|
| `key` | str | stable id (e.g. `wopr`, `skynet`, `glados`); used in stats and clip filenames |
| `wake` | pattern \| None | tolerant regex that summons it while idle; WOPR also summonable by the 2600 whistle |
| `voice` / `speed` / `pitch` | str / int / int | espeak voice for this game |
| `system_prompt` | str | persona for LLM turns (quest chat, or a cutscene's generated message) |
| `greeting` | str | opening line for interactive games |
| `intro` | clip-key \| None | optional per-character intro clip played before the game starts (existing mechanism) |
| `quest` | Quest \| None | present for the Quest archetype |
| `cutscene` | Cutscene \| None | present for the Cutscene archetype |

**Validation**: exactly one of `quest` / `cutscene` may be set (or neither, for a plain chat game or
a utility clip). A Game with neither `quest` nor `cutscene` and no `audio`/utility role is a plain
chat with no win. Utility experiences (greeter, operator, goodbye) are **not** Games and are excluded
from `GAME_KEYS`.

**Archetype rule**: `archetype = "cutscene" if cutscene else "quest" if quest else "chat"`.

## Objective (a.k.a. Beat)

One goal inside a Quest.

| Field | Type | Notes |
|---|---|---|
| `name` | str | machine id (e.g. `joshua`, `prime_directives`) |
| `matcher` | pattern | tolerant regex over a caller utterance (accepts common STT mishears) |
| `label` | str | human label for the funnel/report |

## Quest (content of a Quest game)

| Field | Type | Notes |
|---|---|---|
| `objectives` | list[Objective] | one or more; must be non-empty |
| `ordered` | bool | true = objectives must be satisfied in list order; false = any order |
| `reveal` | str | line spoken on win; `{flag}` is substituted |
| `flag` | str | reward token; must be non-empty |
| `fanfare` | bool | play the wink fanfare before the reveal |
| `reveal_voice`/`speed`/`pitch` | str/int/int \| None | optional distinct voice for the reveal |
| `reveal_intro` | clip-key \| None | optional clip before the reveal (e.g. WOPR's flag-intro) |

**Validation**: `objectives` non-empty and `flag` non-empty, else the Game is rejected at startup and
logged; other games keep running.

## QuestState (per call, transient)

Generalizes the current `MovieQuest`.

| Field | Type | Notes |
|---|---|---|
| `hit` | set[str] | objective names satisfied so far |
| `unlocked` | bool | all objectives satisfied (respecting `ordered`) |

**Transitions**: `update(text)` feeds one utterance; for `ordered` quests, only the next expected
objective can be newly hit; `unlocked` flips true when `hit` covers all objectives. Reset per call
(never persists or leaks between callers).

## Cutscene (content of a Cutscene game)

| Field | Type | Notes |
|---|---|---|
| `clip` | clip-key | the audio to play (e.g. the song, a sampled clip) |
| `message_mode` | enum: `none` \| `fixed` \| `generated` | how the single message is produced |
| `message_text` | str | the fixed line, or the fallback line for `generated` |
| `goodbye` | bool | play the shared goodbye sign-off before ending (default true) |

**Flow** (non-interactive): play `intro` (if any) → play `clip` → speak message per `message_mode`
(with fallback to `message_text`, or skip) → play `goodbye` → **end the call**.

**Validation**: `clip` must resolve to loaded frames at play time; if missing, skip that step rather
than fail the call.

## Loaded audio clips (existing mechanism, referenced by key)

Startup-loaded frame lists keyed by name: per-character `INTRO_FRAMES[key]`, `SONG_FRAMES`,
`OPERATOR_FRAMES`, `GOODBYE_FRAMES`. Quests/cutscenes reference these by key; missing files are
skipped with a log line.

## Call record (per-call metrics, JSONL) — additions

Extends the existing `sessions.jsonl` record.

| Field | Type | Notes |
|---|---|---|
| `games` | map key → {reached, turns, won, **completed**, clip, first_ms} | per-game engagement; `completed` is new (cutscene finished) |
| `games_played` | list[str] | games reached (excludes utility) — existing |
| `primary_game` | str \| None | most-engaged game by turns — existing |
| `reached_chat` | bool | any game had >0 turns — existing |

`archetype` per game is derivable from the Game definition; `won` applies to quests, `completed` to
cutscenes.

## Aggregate DB (`game_stats.json`) — additions

| Field | Type | Notes |
|---|---|---|
| `total_calls` | int | existing |
| `updated` | str | existing |
| `games[key]` | {sessions, turns, wins, **completions**, clips} | `completions` is new, next to `wins` |

Backward compatible: records/aggregates without `completed`/`completions` are read as 0.

## Relationships

```
Experience/Game ──has one──▶ Quest ──has many──▶ Objective
      │           └──or one─▶ Cutscene ──refers──▶ clip-key
      │
      └──engagement per call──▶ Call record.games[key] ──rolls up──▶ game_stats.json games[key]
QuestState (per call) ── derived from ──▶ Quest, feeds ──▶ Call record.games[key].won
```
