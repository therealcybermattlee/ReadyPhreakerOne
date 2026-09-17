---
description: "Task list for the Game Framework feature"
---

# Tasks: Game Framework

**Input**: Design documents from `/specs/002-game-framework/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/game-definition.md

**Tests**: Included. The project has an established `unittest` suite (`tests/test_dsp.py`) and the
quickstart validates via unit tests; each story adds targeted tests for its pure logic.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files / independent, no incomplete deps)
- **[Story]**: US1–US4 from spec.md; Setup/Foundational/Polish carry no story label
- Nearly all code lives in the single-file daemon `wopr_daemon.py`; tests in `tests/test_dsp.py`;
  analytics in `analyze.py`. "Parallel" here means independent edit regions / files.

## Path Conventions

Single project. Daemon: `wopr_daemon.py`. Tests: `tests/test_dsp.py`. Analytics: `analyze.py`.
Audio harness: `harness.py`. Deploy target on the mini: `/opt/wopr/`.

---

## Phase 1: Setup (scaffolding types)

- [X] T001 Add a "Game framework" section header + dataclasses `Objective`, `Quest`, `Cutscene` (fields per data-model.md) near the existing `Experience` dataclass in `wopr_daemon.py`
- [X] T002 Extend the `Experience` dataclass in `wopr_daemon.py` with optional `quest: Quest | None = None` and `cutscene: Cutscene | None = None`, and add an `archetype` property returning `"cutscene" | "quest" | "chat"`
- [X] T003 Update `GAME_KEYS` derivation in `wopr_daemon.py` so it stays "all experiences except greeter/goodbye/operator utility" and add an `is_game(key)` helper

**Checkpoint**: Types compile; `python -c "import ast; ast.parse(...)"` clean; existing tests still pass.

---

## Phase 2: Foundational (blocking prerequisites for all stories)

**⚠️ MUST complete before US1–US4.**

- [X] T004 Generalize `MovieQuest` into a data-driven quest engine in `wopr_daemon.py`: a `QuestState` (`hit`, `unlocked`) whose `update(text)` consumes an `Experience.quest` (objectives + `ordered`), replacing the WOPR-hardcoded beat set; keep a `progress` property for logging
- [X] T005 Add a startup validation pass in `amain` (`wopr_daemon.py`): reject/disable any game whose `quest` has no objectives or empty flag, or that sets both `quest` and `cutscene`; log `event=game_invalid key=…` and continue loading other games
- [X] T006 Add a `run_cutscene(sess)` helper skeleton in `wopr_daemon.py` (clip → message → goodbye → end-call) and a `end_call(sess)` helper that sends the AudioSocket terminate frame (type 0x00) then closes the writer, guarded by the existing single-teardown `sess.closed` flag (per contracts §2 / research D1)
- [X] T007 Refactor `conversation(sess)` in `wopr_daemon.py` to dispatch on `sess.experience.archetype`: `cutscene` → `run_cutscene`; otherwise the existing capture→STT→LLM→TTS loop with the generalized quest engine gating the win. Preserve wink/greeting/intro/barge-in ordering
- [X] T008 [P] Add unit tests in `tests/test_dsp.py` for `QuestState`: any-order completion, ordered completion (out-of-order does not win), tolerant matcher hits, per-call isolation (no leak)

**Checkpoint**: Daemon boots; WOPR still reaches its win through the generalized engine; cutscene skeleton reachable but content not yet migrated.

---

## Phase 3: US1 — Author adds a game declaratively (Priority: P1) 🎯 MVP

**Goal**: Every existing character is expressed as a single declarative Game definition; adding a
game requires only a definition, no call-loop edits. Behavior matches today (no new wins/end-call yet).

**Independent Test**: Add a throwaway Game definition of each archetype, restart, confirm it's
summonable and behaves per archetype; confirm no shared call-loop code changed to add it.

- [X] T009 [US1] Redefine `WOPR_EXPERIENCE` in `wopr_daemon.py` as a `quest=Quest(...)` carrying the existing four objectives (joshua/play/thermonuclear/winning-move), `ordered=False`, the current reveal line + `JOSHUA FIVE BY FIVE` flag, fanfare + Johnny-5 reveal voice + flag-intro clip; keep the whistle/Joshua entry and chess follow-on
- [ ] T010 [P] [US1] Redefine `SKYNET_EXPERIENCE`, `ROBOCOP_EXPERIENCE`, `ET_EXPERIENCE` in `wopr_daemon.py` as quest games with placeholder single-objective quests (content finalized in US2) so they route through the quest path
- [ ] T011 [P] [US1] Redefine `GLADOS_EXPERIENCE` and `TONYHAWK_EXPERIENCE` in `wopr_daemon.py` as `cutscene=Cutscene(...)` referencing the song/tonyhawk clips (message/end-call behavior finalized in US3; for now migrate current behavior)
- [X] T012 [US1] Ensure greeter/operator/goodbye remain utility (neither quest nor cutscene) and are excluded from games in `wopr_daemon.py`
- [X] T013 [US1] Add a unit test in `tests/test_dsp.py` proving a minimal ad-hoc Game definition (one quest, one cutscene) validates and is selectable via `match_experience`, with no edits outside the definition
- [X] T014 [US1] Update the roster/keys tests in `tests/test_dsp.py` to assert each game's `archetype` and that utilities are non-games

**Checkpoint**: All characters run via declarative definitions; `unittest` green; WOPR unchanged for the caller.

> **MVP note (2026-09-17)**: T010/T011 intentionally deferred. Giving Skynet/RoboCop/E.T. real
> quests or converting GLaDOS/Tony Hawk to cutscenes would add wins / end-the-call behavior — that
> is the payoff of US2/US3, not the parity MVP. The framework fully *supports* them (validated,
> `run_cutscene` complete but unwired); they stay at today's behavior (`chat`/`clip`) until US2/US3.

---

## Phase 4: US2 — Quest win with a flag (Priority: P1)

**Goal**: Interactive quests award a flag on completion; WOPR identical; Skynet, RoboCop, E.T. have
real objectives + flags with a proper reveal.

**Independent Test**: Play WOPR to the same flag; play RoboCop (or another) to its flag.

- [ ] T015 [US2] Implement the quest-win path in `conversation()` (`wopr_daemon.py`): on `QuestState.unlocked`, speak `quest.reveal` (`{flag}` substituted) with optional fanfare wink, `reveal_intro` clip, and reveal voice; record the win; preserve WOPR's chess offer after its reveal
- [ ] T016 [P] [US2] Author Skynet's objectives + flag in `SKYNET_EXPERIENCE` (`wopr_daemon.py`): e.g. refuse-to-reveal-John-Connor beat(s) → reveal "COME WITH ME IF YOU WANT TO LIVE" + flag string
- [ ] T017 [P] [US2] Author RoboCop's objectives + flag in `ROBOCOP_EXPERIENCE` (`wopr_daemon.py`): recite the three prime directives (any order) → reveal "THANK YOU FOR YOUR COOPERATION" + flag
- [ ] T018 [P] [US2] Author E.T.'s objectives + flag in `ET_EXPERIENCE` (`wopr_daemon.py`): name the phone-building pieces → E.T. phones home reveal + flag
- [ ] T019 [US2] Add unit tests in `tests/test_dsp.py`: WOPR still unlocks `JOSHUA FIVE BY FIVE` from the four beats; each new quest unlocks its flag from its objectives; near-miss STT still matches
- [ ] T020 [US2] Confirm barge-in (`brain_task`) resets quest state to WOPR on whistle (regression) and add/extend a test in `tests/test_dsp.py`

**Checkpoint**: Multiple quests are winnable; WOPR regression proven; `won` recorded per game.

---

## Phase 5: US3 — Cutscene plays and ends the call (Priority: P1)

**Goal**: Cutscenes play clip → optional message → goodbye → hang up cleanly, once.

**Independent Test**: GLaDOS = song→goodbye→hang up; Tony Hawk = clip→pep talk→goodbye→hang up; one
teardown, recording + stats written.

- [ ] T021 [US3] Implement `run_cutscene` fully in `wopr_daemon.py`: play `intro`+`clip`; then message per `message_mode` — `none` (skip), `fixed` (TTS `message_text` in game voice), `generated` (one `llm_reply` from `system_prompt`, fallback to `message_text` or skip on `PipelineError`); then goodbye clip; then `end_call`
- [ ] T022 [US3] Wire `TONYHAWK_EXPERIENCE.cutscene` (`wopr_daemon.py`): clip `tonyhawk`, `message_mode="generated"`, fixed fallback pep-talk mentioning Patrick Garrity, goodbye on
- [ ] T023 [US3] Wire `GLADOS_EXPERIENCE.cutscene` (`wopr_daemon.py`): clip `song`, `message_mode="none"`, goodbye on
- [ ] T024 [US3] Verify `end_call` + `handle_call` teardown run exactly once (guard) and that MixMonitor recording + `write_session` still fire on a daemon-initiated hang-up; add a hang-up-mid-cutscene guard test in `tests/test_dsp.py` (mock writer)
- [ ] T025 [P] [US3] Add unit tests in `tests/test_dsp.py` for the message-mode selection and fallback (none/fixed/generated→fallback) without invoking real TTS/LLM (inject stubs)

**Checkpoint**: Cutscenes end the call cleanly with content; caller-hangup mid-cutscene is safe.

---

## Phase 6: US4 — Analytics: wins vs completions (Priority: P2)

**Goal**: Per-archetype completion signal recorded and reported; backward compatible.

**Independent Test**: After mixed calls, analytics show quest **wins** and cutscene **completions**
per game, with no errors on legacy records.

- [ ] T026 [US4] Extend `note_game()` and `metrics()` in `wopr_daemon.py` to set/emit `completed` for cutscenes (set at the end-call step) alongside `won`
- [ ] T027 [US4] Extend `update_stats_db()` in `wopr_daemon.py` to increment a `completions` counter beside `wins` per game (absent → 0)
- [ ] T028 [P] [US4] Extend `analyze.py` per-game table to show a `completions` column beside `wins`, tolerant of records/aggregates lacking the field
- [ ] T029 [P] [US4] Add unit tests in `tests/test_dsp.py` for `completed`/`completions` (quest→won, cutscene→completed) and legacy-record tolerance

**Checkpoint**: Stats distinguish wins from completions end to end.

---

## Phase 7: Polish & Cross-Cutting

- [ ] T030 Run the full `unittest` suite locally and fix any regressions (`tests/test_dsp.py`)
- [ ] T031 Deploy to the mini (scp `wopr_daemon.py` + `analyze.py` to `/opt/wopr`, restart the LaunchDaemon) and confirm a clean boot (`event=listen`, no `game_invalid`)
- [ ] T032 Run `harness.py` (stub daemon on 9093) to confirm the 20 ms pacing and detector are unaffected by the framework changes
- [ ] T033 Walk quickstart.md steps 2–7 on the mini: WOPR regression, a new quest win, both cutscenes hang up once, analytics wins vs completions, offline check
- [ ] T034 [P] Update `README.md` to describe the two game archetypes and how to add a game (one Game definition), and note the new stats fields
- [ ] T035 [P] Update `analyze.py`/docs references and commit; push to `therealcybermattlee/ReadyPhreakerOne`

---

## Dependencies & Execution Order

- **Setup (T001–T003)** → blocks everything.
- **Foundational (T004–T008)** → blocks all user stories (quest engine, dispatch, cutscene/end-call helpers, validation).
- **US1 (T009–T014)** → MVP; migrates all characters to declarative definitions (behavior parity). Depends on Foundational.
- **US2 (T015–T020)** and **US3 (T021–T025)** → both P1, both depend on US1; independent of each other (quest path vs cutscene path) and can proceed in parallel.
- **US4 (T026–T029)** → P2; depends on US2 (`won`) and US3 (`completed`) existing.
- **Polish (T030–T035)** → after the stories.

## Parallel Opportunities

- Within US1: T010 and T011 (different experiences) are `[P]`.
- Within US2: T016/T017/T018 (Skynet/RoboCop/E.T. content) are `[P]`.
- US2 and US3 as whole phases can run in parallel after US1.
- Test-writing tasks marked `[P]` touch `tests/test_dsp.py` — serialize edits to that one file even though they're logically independent.

## Independent Test Criteria (per story)

- **US1**: add a throwaway game of each archetype → summonable + correct archetype behavior, no shared-loop edits.
- **US2**: WOPR reveals the same flag; a new quest reveals its flag.
- **US3**: each cutscene plays content and hangs up once, recording + stats written.
- **US4**: analytics separate wins (quests) from completions (cutscenes), no error on legacy data.

## Suggested MVP

**Setup + Foundational + US1** — the whole roster runs through the declarative framework with today's
behavior. US2 (new quest wins) and US3 (cutscene hang-up) are the next increments; US4 (analytics)
last.
