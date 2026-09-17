# Phase 0 Research: Game Framework

Design decisions resolving the plan's open questions. No `NEEDS CLARIFICATION` remained in the
Technical Context; the items below are the substantive design choices.

## D1. How a Cutscene ends the call cleanly

**Decision**: After the cutscene's clip → message → goodbye, the daemon ends its own side of the
AudioSocket by sending a **terminate frame** (type `0x00`, per the AudioSocket contract) and then
closing the writer. Asterisk sees the hang-up and returns the payphone to on-hook. The existing
`CallEnded`/teardown path in `handle_call` finalizes recording (MixMonitor is Asterisk-side and
closes on hang-up) and calls `write_session`, so **stats are written exactly once** on the normal
teardown, whether the caller hangs up or the cutscene ends the call.

**Rationale**: Reuses the one teardown path already proven for caller-initiated hang-ups; avoids a
second stats-writing site (which would risk double-counting). Terminating from the daemon is the
minimal, protocol-native way to drop the call.

**Alternatives considered**: (a) Asterisk-side `Hangup()` after a fixed time — rejected: couples the
lifecycle to the dialplan (out of scope) and can't align to the exact end of a variable-length clip.
(b) Just stop sending audio and wait for the caller to hang up — rejected: leaves the line open in
silence, defeating "end the call". (c) Writing stats at the cutscene site AND on teardown — rejected:
double-write risk.

**Edge**: the terminate must be idempotent with a concurrent caller hang-up — guard so teardown runs
once (existing `sess.closed` flag).

## D2. Generalizing `MovieQuest` into a reusable Quest engine

**Decision**: Extract WOPR's hard-coded quest into a `Quest` value object carried by a Game:
an ordered/any-order list of **Objectives** (name, tolerant regex matcher, label), a reveal line
(with `{flag}` substitution), a flag token, and optional reveal presentation (fanfare wink, reveal
voice/speed/pitch, intro clip). A single `QuestState` per call tracks hit objectives and win, replacing
the WOPR-specific `MovieQuest`. WOPR is defined as the existing four beats + `JOSHUA FIVE BY FIVE` +
its chess follow-on, producing byte-identical behavior.

**Rationale**: The current `MovieQuest` already models "beats matched by tolerant regex → unlocked";
promoting its fields to data on the Game is a direct generalization, not a rewrite. Ordered vs
any-order is a single flag.

**Alternatives considered**: A rules/DSL engine for objectives — rejected as over-engineered for a
handful of games; plain regex matchers already handle the STT-mishear tolerance.

**Note (content)**: WOPR's chess Easter egg stays WOPR-specific content hanging off its win, not a
generic archetype feature.

## D3. Cutscene message: fixed vs generated, and fallback

**Decision**: A Cutscene's message is one of: **none**, a **fixed** line (spoken via the game's
voice), or a **single generated** line (one LLM completion using the game's persona prompt — no
capture/turn loop). If a generated message fails or times out, the cutscene falls back to a declared
fixed line, or skips the message if none is declared, and always proceeds to goodbye → end call.

**Rationale**: Tony Hawk wants a lively, non-repeating pep talk (generated) but must never hang the
call; GLaDOS wants no message at all. A one-shot generation reuses `llm_reply` with the persona
prompt and `num_predict`, with the existing `PipelineError` handling as the fallback trigger.

**Alternatives considered**: Always-fixed messages — rejected: loses Tony's variety. Multi-turn for
cutscenes — rejected: contradicts the "no back-and-forth, then hang up" requirement.

## D4. Where the completion signal lives in analytics

**Decision**: Reuse the existing `note_game()` engagement tracker. Add `completed` alongside the
current `won`: quests set `won` on flag unlock (as today); cutscenes set `completed` when they reach
the end-call step. The per-call `metrics()` and the `game_stats.json` aggregate gain a `completions`
count next to `wins`. `analyze.py` (which already renders the per-game table) shows both columns.

**Rationale**: The stats plumbing (`games` dict, `note_game`, aggregate RMW) already exists and is
tested; this is an additive field, backward-compatible with older records (missing → treated as 0).

**Alternatives considered**: A separate cutscene log — rejected: fragments the one-record-per-call
model and the aggregate DB.

## D5. Declaring games without touching the call loop

**Decision**: Extend the existing `Experience` dataclass so a game carries either `quest=Quest(...)`
or `cutscene=Cutscene(...)` (mutually exclusive; utility clips like greeter/operator/goodbye carry
neither and are excluded from `GAME_KEYS`). `conversation()` dispatches: cutscene → clip/message/
goodbye/end; quest/plain-chat → the existing capture→STT→LLM→TTS loop with the game's quest engine
gating the win. Adding a game = appending one `Experience` with its `quest`/`cutscene` content and a
wake phrase; no changes to `brain_task`, the idle listener, recording, or stats.

**Rationale**: `Experience` is already the per-character declaration and the dispatch point; the
archetype is one more field. Keeps the "single declarative definition" requirement (FR-001/003).

**Alternatives considered**: A registry/plugin system with separate files — rejected: the daemon is
deliberately one file; a dataclass list is simpler and testable.

## D6. Preserving idle summon, whistle, and barge-in

**Decision**: Unchanged. Wake phrases are matched only in the idle/ARMED state by the existing idle
listener; the 2600 Hz whistle still forces WOPR (including barge-in mid-game). A cutscene is summoned
like any game (wake phrase in idle) and simply doesn't enter the capture loop.

**Rationale**: These behaviors are orthogonal to archetype and already correct; the framework must
not disturb them (FR-015/016).

## Summary of decisions

| # | Decision |
|---|---|
| D1 | Cutscene ends the call via an AudioSocket terminate frame; stats written once on normal teardown |
| D2 | `MovieQuest` generalized to a data-driven `Quest` (objectives + order + reveal + flag); WOPR unchanged |
| D3 | Cutscene message: none / fixed / one-shot generated, with fixed fallback; never hangs |
| D4 | Add `completed` next to `won`; extend per-call + aggregate stats and `analyze.py` |
| D5 | Archetype is a field on `Experience` (`quest` xor `cutscene`); `conversation()` dispatches; no call-loop edits to add a game |
| D6 | Idle summon, whistle→WOPR, and barge-in preserved unchanged |
