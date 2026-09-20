# Contract: Directory Authoring & Operator Reply Guarantees

## 1. Directory entry (authoring contract)

A listed game declares its entry next to its wake phrase. An easter egg declares `listed=False`
and no entry.

```
ROBOCOP_EXPERIENCE = Experience(
  "robocop", ..., wake=re.compile(r"\b(robo\s*cop|murphy|dead or alive)\b", re.I),
  directory=DirectoryEntry(
      name="RoboCop",
      say=("RoboCop", "Murphy"),
      blurb="a cyborg officer who will process you through his prime directives"),
  quest=ROBOCOP_QUEST)

HAL_EXPERIENCE = Experience(
  "hal", wake=re.compile(r"\bhal\b|\bpod\s+bay\b", re.I),
  listed=False,                      # easter egg: never listed, hinted, or confirmed
  cutscene=Cutscene(clip="hal-disconnect"))
```

**Rules**
- `listed=True` games MUST have a `directory` entry; `listed=False` MUST NOT (validated at startup, logged, non-fatal).
- `say` values MUST each match the game's own `wake` regex (validated: a `say` that would not summon its game is logged).
- Utilities (`greeter`, `goodbye`, `operator`) are never in the directory.
- Adding a game = one `Experience` with its entry; the Operator's prompt and filter update automatically.

## 2. What the Operator will and will not say (reply guarantees)

Given any caller utterance, the spoken reply satisfies all of:

1. **Closed world**: every character name spoken is the `name` of a listed game. Any other
   character-like name (an easter egg, a hallucination) is removed before speech; an emptied reply
   is replaced by the fixed fallback line.
2. **Secret word never spoken**: the reply contains neither the secret word nor any spelling the
   secret-word wake regex accepts; a violating reply is replaced by the brush-off line.
3. **Clues are ordered and deterministic**: a clue request gets `OPERATOR_CLUES[n]` for the n-th
   request in the call, then the exhausted line; no model involvement.
4. **Bounded**: replies are ≤ 2 sentences after `postprocess_reply`; a reply exceeding the reply
   budget (default 5 s end to end) is abandoned for the fixed fallback line.
5. **Deterministic escape hatches**: "list the options" → the recorded hints clip; a listed
   character's name → immediate connection; "main menu" → the Operator herself (no-op
   acknowledgement); "goodbye" → sign-off and hang-up; a whistle → back to the Operator.
6. **Nudge cadence**: with no caller speech, one nudge per `idle_nudge_s` (12 s), measured from
   the end of her last speech.

## 3. Degraded-mode contract

- `--no-operator` / `WOPR_OPERATOR=0`, `--stub-llm`, or an Ollama warmup that never succeeded ⇒
  after the seize, and on every return to the menu, the daemon behaves exactly as before this
  feature: the recorded menu loop plays in ARMED and wake phrases summon games.
- The switch is evaluated per return-to-menu, so an Ollama that comes back mid-call is used on the
  next return.

## 4. Invariants preserved

- The 2600 gate, whistle handling, barge-in, recording, and per-game stats are unchanged.
- The Operator never appears in `GAME_KEYS`, `games_played`, `primary_game`, or the aggregate DB.
- Every game's wake phrase works from the Operator exactly as it did from the menu loop.
