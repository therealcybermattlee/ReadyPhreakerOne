# Contract: Game Definition & Call-End

The "interface" this feature exposes is the **declarative Game definition** an author writes to add a
game, plus the **call-end contract** for cutscenes. These are the stable surfaces the rest of the
daemon depends on.

## 1. Game definition (authoring contract)

A game is one declaration in the daemon's experience list. Exactly one archetype block is present.

### Quest game

```
Game(
  key            = "robocop",                     # unique; also the stats key and clip prefix
  wake           = r"\b(robo\s?cop|murphy|dead or alive)\b",   # tolerant; matched only while idle
  voice="en-us+klatt", speed=133, pitch=30,
  system_prompt  = "<persona for chat turns>",
  greeting       = "<opening line>",
  intro          = "robocop",                     # optional: plays robocop-intro.wav first
  quest = Quest(
    objectives = [
      Objective("serve",   r"serve the public", "Serve the public trust"),
      Objective("protect", r"protect the innocent", "Protect the innocent"),
      Objective("uphold",  r"uphold the law", "Uphold the law"),
    ],
    ordered      = False,                          # any order (or True for a fixed sequence)
    reveal       = "THANK YOU FOR YOUR COOPERATION. YOUR FLAG IS {flag}.",
    flag         = "PRIME-DIRECTIVE-4-CLASSIFIED",
    fanfare      = True,
    reveal_voice = None,                           # None = keep the game's voice
  ),
)
```

**Rules**
- `key` is unique across games; utility clips (greeter/operator/goodbye) are **not** games.
- `wake` is honored only in the idle state. WOPR additionally responds to the 2600 Hz whistle.
- `quest.objectives` is non-empty; `quest.flag` is non-empty. Violations are logged at startup and
  the game is disabled; other games keep running.
- `ordered=True` means an objective can only be newly satisfied when it is the next unmet one.
- `{flag}` in `reveal` is substituted with `flag`.

### Cutscene game

```
Game(
  key           = "tonyhawk",
  wake          = r"\b(kick\s?flip|tony hawk|ollie)\b",
  voice="en-us+m3", speed=165, pitch=55,
  system_prompt = "<Tony Hawk persona, for the generated pep talk>",
  intro         = "tonyhawk",                      # optional: plays tonyhawk-intro.wav first
  cutscene = Cutscene(
    clip         = "tonyhawk",                      # required audio clip key
    message_mode = "generated",                     # none | fixed | generated
    message_text = "YOU CAN LAND IT. SHRED LIKE PATRICK GARRITY.",  # fixed line, or fallback for generated
    goodbye      = True,
  ),
)

Game(
  key           = "glados",
  wake          = r"\b(the )?cake is a lie\b",
  cutscene = Cutscene(clip="song", message_mode="none", goodbye=True),
)
```

**Rules**
- Exactly one of `quest` / `cutscene` is set.
- `message_mode`:
  - `none` — no message; play clip → goodbye → end.
  - `fixed` — speak `message_text` in the game's voice.
  - `generated` — one LLM completion from `system_prompt`; on failure/timeout fall back to
    `message_text` (or skip if empty). **Never** a multi-turn exchange.
- Missing `clip`/`intro`/`goodbye` audio is skipped (logged), never fatal.

## 2. Cutscene call-end contract

After `clip → message → goodbye`, the daemon ends the call:

1. Send an AudioSocket **terminate frame** (type `0x00`) to Asterisk, then close the writer.
   (See `specs/001-whistle-wopr-daemon/contracts/audiosocket-protocol.md` for frame types.)
2. The single existing teardown path runs exactly once (guarded), which:
   - lets Asterisk finalize the MixMonitor recording (both legs) on hang-up, and
   - writes the per-call `sessions.jsonl` record and updates `game_stats.json`.
3. A caller who hangs up **during** the cutscene triggers the same teardown once; the cutscene's
   pending steps are abandoned and the game is recorded as reached-but-not-completed.

**Guarantee**: stats and recording are written exactly once per call regardless of who ends it.

## 3. Stats record contract (additions)

Per-call `games[key]` gains `completed: bool`. Aggregate `game_stats.json` `games[key]` gains
`completions: int` beside `wins`. Consumers (e.g. `analyze.py`) MUST treat absent
`completed`/`completions` as `false`/`0` for backward compatibility.

## 4. Invariants preserved (regression contract)

- Greeter menu on pickup, `operator` hints, and the standalone `goodbye` sign-off behave as before
  and are not games.
- WOPR: same four beats → `<WOPR flag>`, same chess follow-on, same whistle/Joshua entry,
  same barge-in on whistle.
- Per-call recording and analytics run for every game and archetype.
- No network access at call time.
