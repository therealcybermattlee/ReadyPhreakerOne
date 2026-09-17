# Feature Specification: Game Framework

**Feature Branch**: `002-game-framework`

**Created**: 2026-09-16

**Status**: Draft

**Input**: User description: "A Game Framework for the ReadyPhreakerOne phreaking phone, so every playable Game has a defined, reusable shape and new games can be added by declaring one definition without touching the core call loop. Two archetypes: interactive Quest games (objectives → win → flag, like WOPR) and non-interactive Cutscene games (play a clip → optional message → goodbye → end the call, like GLaDOS and Tony Hawk)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Author adds a new game declaratively (Priority: P1)

The installation owner wants to add a new character/game to the phone. They write a single **Game definition** — its name, the wake phrase(s) that summon it, its voice, and its archetype-specific content (either a set of objectives + a flag, or a clip + optional message + goodbye) — and the game works end to end without editing the call state machine, the idle/greeter logic, the whistle path, recording, or analytics.

**Why this priority**: This is the whole point of the feature — turning ad-hoc, hand-wired characters into a uniform, extensible framework. Every other story depends on this abstraction existing.

**Independent Test**: Add a throwaway game definition of each archetype, restart the daemon, and confirm the game is summonable and behaves per its archetype — with zero changes to shared call-loop code.

**Acceptance Scenarios**:

1. **Given** a new Quest game definition with three objectives and a flag, **When** a caller summons it and satisfies all objectives, **Then** the character reveals the flag and the call is recorded as won — without any change to shared code.
2. **Given** a new Cutscene game definition with a clip and a message, **When** a caller summons it, **Then** the clip plays, the message is spoken, the goodbye plays, and the call ends — without any change to shared code.
3. **Given** a malformed or incomplete game definition, **When** the daemon starts, **Then** it reports the problem clearly and the other games continue to work.

---

### User Story 2 - Caller plays an interactive Quest game and wins (Priority: P1)

A caller summons an interactive character (e.g. WOPR) and holds a conversation in that character's voice. The game silently tracks progress toward a set of objectives ("beats") matched from what the caller says. When every objective is met, the character delivers a reveal line and grants a flag (a shareable token), optionally with a fanfare and a distinct reveal voice. WOPR's existing WarGames quest is preserved exactly; Skynet, RoboCop, and E.T. become quest games with their own objectives and flags.

**Why this priority**: Interactive wins are the marquee experience and the existing behavior that must not regress.

**Independent Test**: Play WOPR to completion and confirm the identical flag reveal as today; play one newly-defined quest game (e.g. RoboCop) to completion and confirm its flag reveal.

**Acceptance Scenarios**:

1. **Given** WOPR, **When** the caller re-enacts the four WarGames beats in any order, **Then** WOPR reveals `JOSHUA FIVE BY FIVE` exactly as before and the call is marked won.
2. **Given** a quest game whose objectives are ordered, **When** the caller satisfies them out of order, **Then** the game does not award the win until the objectives are satisfied in the required order.
3. **Given** any quest game, **When** the caller never completes the objectives and hangs up, **Then** the call is recorded with the objectives reached but not won, and progress does not leak between calls.
4. **Given** a caller mid-quest, **When** they whistle 2600 Hz, **Then** the barge-in restarts as WOPR with fresh quest progress (existing behavior preserved).

---

### User Story 3 - Caller triggers a Cutscene game that plays and ends the call (Priority: P1)

A caller summons a non-interactive character. It plays an intro/clip (a song or sampled audio), optionally speaks a single message (fixed line or one generated line, with no back-and-forth), plays the goodbye sign-off, and then ends the call so the payphone hangs up. GLaDOS plays "Still Alive" and then says goodbye; Tony Hawk plays the kickflip clip, delivers the pep talk about becoming a skater like Patrick Garrity, then says goodbye.

**Why this priority**: This is the new archetype the owner explicitly asked for, and it changes call lifecycle (the daemon must end a call cleanly).

**Independent Test**: Summon GLaDOS and confirm song → goodbye → hang up; summon Tony Hawk and confirm clip → pep-talk message → goodbye → hang up. Confirm the recording and analytics for each are written before teardown.

**Acceptance Scenarios**:

1. **Given** GLaDOS (cutscene, no message), **When** summoned, **Then** the song plays, the goodbye plays, and the call ends with no prompt for caller input.
2. **Given** Tony Hawk (cutscene, message), **When** summoned, **Then** the clip plays, the pep-talk message is spoken, the goodbye plays, and the call ends.
3. **Given** any cutscene, **When** it finishes and ends the call, **Then** the call recording is finalized and the per-call/aggregate analytics record the game as completed.
4. **Given** a caller who hangs up during a cutscene, **When** the line drops, **Then** the daemon tears down cleanly and records the game as started-but-not-completed.

---

### User Story 4 - Analytics categorize wins and completions per game (Priority: P2)

The owner reviews per-game statistics. Every game reports a per-archetype completion signal: quest games report **won** (objectives met), cutscene games report **completed** (played through to the end call). The existing per-call records and aggregate database keep working and distinguish the two.

**Why this priority**: The stats already exist; they must stay correct as the notion of "finishing a game" generalizes beyond WOPR's win.

**Independent Test**: Run the analysis after a mix of calls and confirm each game shows the right counts of players, turns, wins (quest), and completions (cutscene).

**Acceptance Scenarios**:

1. **Given** a completed quest game, **When** analytics run, **Then** it is counted as a win for that game.
2. **Given** a completed cutscene, **When** analytics run, **Then** it is counted as a completion (not a win) for that game.
3. **Given** older call records without the new fields, **When** analytics run, **Then** they are handled gracefully without error.

---

### Edge Cases

- **Local STT mishears an objective**: objective matchers must be tolerant of common mishearings (the same tolerance WOPR's beats already use); a near-miss should still count.
- **Caller says a different game's wake phrase mid-conversation**: wake phrases are only honored while the phone is idle, so mid-game utterances do not switch games (existing behavior); the whistle remains the one exception that barges in to WOPR.
- **Cutscene message generation is unavailable** (e.g. the LLM is slow/unreachable): a cutscene with a *fixed* message must still play; a cutscene with a *generated* message must fall back to a safe fixed line or skip the message rather than hang the call.
- **A game's intro or clip file is missing**: the game still runs (skips the missing audio) rather than failing the call.
- **Ending a cutscene call races with a caller hangup**: teardown, recording finalization, and analytics writing must each complete exactly once.
- **A quest game defines zero objectives or an empty flag**: the daemon rejects/flags the definition at startup rather than creating an unwinnable or reward-less game.

## Requirements *(mandatory)*

### Functional Requirements

**Framework & authoring**

- **FR-001**: The system MUST represent every playable game as a single declarative **Game definition** that specifies: a stable key/name, one or more wake phrases (and, for WOPR only, the 2600 Hz whistle), a voice, an optional pre-game intro clip, an archetype, and the archetype-specific content.
- **FR-002**: The system MUST support exactly two archetypes: **Quest** (interactive, objective-driven, with a win and a flag) and **Cutscene** (non-interactive: clip → optional message → goodbye → end call).
- **FR-003**: Adding, removing, or editing a game MUST NOT require changes to the shared call state machine, idle/greeter logic, whistle detection, recording, or analytics — only the game's own definition.
- **FR-004**: Per-game content that an author would tune (flags, reveal lines, objective phrases, messages, voices, clip paths) MUST be editable as declarations/configuration, not buried in control flow.
- **FR-005**: The system MUST validate game definitions at startup and report any invalid game (e.g. a quest with no objectives or no flag, an unknown archetype) without preventing the other games from running.

**Quest archetype**

- **FR-006**: A Quest game MUST hold a caller in a multi-turn conversation in the game's voice and silently track progress toward its objectives, each matched from caller utterances using tolerant matching that tolerates local speech-to-text mishearings.
- **FR-007**: A Quest game MUST support objectives that are required in a fixed order OR in any order, per the game's declaration.
- **FR-008**: When all objectives are met, a Quest game MUST mark the game won, speak its reveal line, and grant its flag, optionally with a fanfare and a distinct reveal voice/clip.
- **FR-009**: WOPR's existing quest (four WarGames beats → `JOSHUA FIVE BY FIVE`, its chess Easter egg, and its whistle/Joshua entry) MUST behave identically after the framework is introduced.
- **FR-010**: Quest progress MUST be per call and MUST NOT leak between callers or persist across calls.

**Cutscene archetype**

- **FR-011**: A Cutscene game MUST, on summon, play its clip, then (if declared) deliver a single message (fixed text or one generated line, with no caller back-and-forth), then play the goodbye sign-off, then end the call so the payphone hangs up.
- **FR-012**: A Cutscene's message MUST be optional; a cutscene with no message plays clip → goodbye → end call.
- **FR-013**: When a Cutscene ends the call, the system MUST finalize the call's recording and write its analytics before or as part of teardown, exactly once.
- **FR-014**: If a Cutscene's message cannot be produced (generation unavailable), the system MUST still complete the cutscene (fixed fallback or skip the message) rather than hang.

**Preserved behavior**

- **FR-015**: The greeter menu on pickup, the "operator" hints clip, and the standalone goodbye sign-off MUST continue to work unchanged and are NOT themselves games.
- **FR-016**: Wake phrases MUST summon a game only while the phone is idle; the 2600 Hz whistle MUST continue to summon WOPR, including as a barge-in mid-game.
- **FR-017**: Per-call recording (both call legs) and the per-call and aggregate analytics MUST continue to function for every game and every archetype.
- **FR-018**: The system MUST continue to operate entirely locally with no internet dependency at call time.

**Analytics**

- **FR-019**: The system MUST record a per-archetype completion signal for every game: Quest games record **won**; Cutscene games record **completed**.
- **FR-020**: Analytics MUST distinguish, per game, players who reached it, conversation turns, quest wins, and cutscene completions, and MUST handle historical records that predate these fields without error.

### Key Entities *(include if feature involves data)*

- **Game**: A playable experience. Attributes: key/name, wake phrase(s), summon voice, optional intro clip, archetype (Quest or Cutscene), and archetype content. The greeter/operator/goodbye clips are host/utility, not Games.
- **Objective (Beat)**: One goal within a Quest — a name, a tolerant matcher over caller speech, a display label, and its place in the (optional) required order.
- **Quest content**: The set of Objectives, the order requirement, the reveal line, the flag/reward token, and optional reveal presentation (fanfare, voice, clip).
- **Cutscene content**: The clip to play, the optional message (fixed or generated) and its fallback, and the reference to the goodbye sign-off.
- **Call record**: Per call — which games were reached, turns per game, quest wins, cutscene completions, timings — feeding the per-call log and the aggregate game database.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An author can add a new game of either archetype by writing only its Game definition (no edits to shared call-loop, idle, recording, or analytics code), and it is summonable after a restart.
- **SC-002**: WOPR plays identically to before — the same four-beat path reveals the same flag — with no observable regression.
- **SC-003**: At least three previously chat-only characters (Skynet, RoboCop, E.T.) each have a defined win condition and flag, and a caller can reach each win.
- **SC-004**: A Cutscene game (GLaDOS, Tony Hawk) plays its clip and message, says goodbye, and ends the call, with the payphone returning to on-hook every time.
- **SC-005**: 100% of cutscene-ended calls finalize their recording and write their analytics record.
- **SC-006**: Per-game analytics correctly separate quest **wins** from cutscene **completions**, and report zero errors on a dataset that mixes new and legacy records.
- **SC-007**: The full experience continues to run with no network access at call time.

## Assumptions

- The existing daemon architecture (idle greeter + wake-phrase/whistle summoning + per-call recording + local STT/LLM/TTS) is the substrate; this feature restructures how characters are defined and adds the cutscene lifecycle, not the transport or hardware.
- The per-character intro-clip mechanism and the per-call/aggregate analytics already added remain in place and are extended, not replaced.
- Cutscene "end the call" means the daemon proactively terminates its side so the telephone hangs up; the caller may also hang up first at any time.
- Objective matchers reuse the same tolerant-matching approach as WOPR's existing beats (accepting common local-STT mishearings of proper nouns).
- Flags are shareable tokens for the installation's game/CTF framing; their exact strings are author-chosen content, not fixed by this spec.
- The specific objective sets and flags for Skynet, RoboCop, and E.T. are illustrative in this spec and will be finalized as content during planning/implementation.

## Out of Scope

- Hardware, ATA, and Asterisk dialplan changes.
- The disabled WOPR "callback" (phone-rings-you-back) feature.
- Converting the greeter, operator hints, or goodbye utility clips into games.
- Persisting per-caller identity, scores, or leaderboards across calls beyond the existing aggregate counts.
