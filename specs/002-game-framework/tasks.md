---
description: "Task list for the Game Framework feature"
---

# Tasks: Game Framework

**Input**: Design documents from `/specs/002-game-framework/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/game-definition.md, quickstart.md

**Tests**: Included. The project has an established `unittest` suite (`tests/test_dsp.py`, 76 green
as of 2026-09-18) and quickstart.md step 1 validates via unit tests; each story adds targeted tests
for its pure logic (no real STT/LLM/TTS in tests — inject stubs).

**Regenerated 2026-09-18** against the code on `main` (through commit `abac63d` "Arcade mode").
Task IDs T001–T035 are kept for traceability with the implement commits; `[X]` marks were verified
against `wopr_daemon.py` / `tests/test_dsp.py`, not carried over blindly. T036–T040 are new.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files / independent edit regions, no incomplete deps)
- **[Story]**: US1–US4 from spec.md; Setup/Foundational/Polish carry no story label
- Nearly all code lives in the single-file daemon `wopr_daemon.py`; tests in `tests/test_dsp.py`;
  analytics in `analyze.py`. "Parallel" means independent edit regions / files; serialize edits to
  the same file in practice.

## Path Conventions

Single project. Daemon: `wopr_daemon.py`. Tests: `tests/test_dsp.py`. Analytics: `analyze.py`.
Audio harness: `harness.py`. Docs: `README.md`. Deploy target on the mini: `/opt/wopr/`.
Run tests with `./venv/bin/python -m unittest tests.test_dsp`.

## Design drift to know about (Arcade mode, commit `abac63d`)

The spec (US3, FR-011, SC-004) and contracts §2 say a Cutscene ends the call after its goodbye. The
shipped code instead runs **arcade mode**: GLaDOS and Tony Hawk play and then **return to the menu**
(`Cutscene.end_call=False`, the default), "main menu"/"go back"/"next game" said inside any game
returns to the menu (`MENU_RETURN_RE`), and the explicit exit is the `goodbye` experience, which is
now a Cutscene with `end_call=True` that plays the sign-off clip and hangs up. Earned flags persist
for the whole call and a spoken tally (`flag_tally`) follows each quest win. The framework still
supports end-the-call cutscenes (the field exists and is tested); it is the *content* decision that
changed. Phase 8 (T036–T040) reconciles the design docs with this decision rather than reverting it.

---

## Phase 1: Setup (scaffolding types)

- [X] T001 Add a "Game framework" section header + dataclasses `Objective`, `Quest`, `Cutscene` (fields per data-model.md) near the existing `Experience` dataclass in `wopr_daemon.py`
- [X] T002 Extend the `Experience` dataclass in `wopr_daemon.py` with optional `quest: Quest | None = None` and `cutscene: Cutscene | None = None`, and add an `archetype` property returning `"cutscene" | "quest" | "clip" | "chat"`
- [X] T003 Update `GAME_KEYS` derivation in `wopr_daemon.py` so it stays "all experiences except greeter/goodbye utility" and add an `is_game(key)` helper

**Checkpoint**: Types compile; existing tests still pass. ✅ Done (commit `f1ae697`).

---

## Phase 2: Foundational (blocking prerequisites for all stories)

**⚠️ MUST complete before US1–US4.**

- [X] T004 Generalize `MovieQuest` into a data-driven quest engine in `wopr_daemon.py`: `QuestState(quest, hit, unlocked)` whose `update(text)` consumes `Quest.objectives` (any-order or `ordered`), with a `progress` property for logging
- [X] T005 Add a startup validation pass `validate_games(log)` called from `amain` (`wopr_daemon.py`): log `event=game_invalid key=… why=…` for a game with both `quest` and `cutscene`, a quest with no objectives, or a cutscene that produces nothing; continue loading the other games
- [X] T006 Add `end_call(sess)` in `wopr_daemon.py` that sends the AudioSocket terminate frame (type 0x00) and drains, tolerant of a broken writer; teardown/recording/stats still run exactly once via `handle_call`'s guarded `finally` (research D1)
- [X] T007 Refactor `conversation(sess)` in `wopr_daemon.py` to dispatch on the experience: `cutscene` → `run_cutscene`; `audio` clip → play-and-return; otherwise the capture→STT→LLM→TTS loop with `QuestState` re-created when the active game changes. Wink/greeting/intro/barge-in ordering preserved
- [X] T008 [P] Add unit tests in `tests/test_dsp.py` (`GameFrameworkTests`) for `QuestState`: none-quest never unlocks, any-order completion, ordered completion (out-of-order does not win)

**Checkpoint**: Daemon boots; WOPR reaches its win through the generalized engine. ✅ Done (commit `f1ae697`).

---

## Phase 3: US1 — Author adds a game declaratively (Priority: P1) 🎯 MVP

**Goal**: Every character is expressed as a single declarative Game definition; adding a game
requires only a definition, no call-loop edits.

**Independent Test**: Add a throwaway Game definition of each archetype, restart, confirm it's
summonable and behaves per archetype; confirm no shared call-loop code changed to add it.

- [X] T009 [US1] Redefine `WOPR_EXPERIENCE` in `wopr_daemon.py` with `quest=WOPR_QUEST` (four beats from `MOVIE_BEATS`, `ordered=False`, `fanfare=True`; `reveal`/`flag` left empty so WOPR keeps the env-overridable `cfg.flag_text`/`cfg.flag`)
- [X] T010 [US1] Give `SKYNET_EXPERIENCE`, `ROBOCOP_EXPERIENCE`, `ET_EXPERIENCE` in `wopr_daemon.py` a `quest=Quest(...)` so they route through the quest archetype (their real objectives/flags are authored in T016–T018; this task may land together with those). Until T015 lands, the win path is WOPR-only, so a non-WOPR quest is harmless but unwinnable — do T015 in the same change set
- [X] T011 [P] [US1] Redefine `GLADOS_EXPERIENCE` and `TONYHAWK_EXPERIENCE` in `wopr_daemon.py` as `cutscene=Cutscene(...)` (GLaDOS: `clip="song"`, no message; Tony Hawk: intro clip as the audio, `message_mode="generated"` with a fixed fallback)
- [X] T012 [US1] Ensure greeter/goodbye remain utility (excluded from `GAME_KEYS`) in `wopr_daemon.py`; the greeter is an `audio="operator"` clip, goodbye is the exit cutscene
- [X] T013 [US1] Unit test in `tests/test_dsp.py` (`test_minimal_game_definitions_validate`) proving a minimal ad-hoc Game of each archetype validates and is selectable via `match_experience` with no edits outside the definition
- [X] T014 [US1] Roster tests in `tests/test_dsp.py` (`test_archetypes`, `test_shipped_roster_has_no_invalid_games`) asserting each game's `archetype` and that utilities are non-games

**Checkpoint**: All characters run via declarative definitions; WOPR unchanged for the caller.
✅ MVP shipped at parity (commit `f1ae697`); only T010 remains and is folded into US2.

---

## Phase 4: US2 — Quest win with a flag (Priority: P1)

**Goal**: Interactive quests award a flag on completion for *any* quest game, not just WOPR; WOPR
is byte-identical; Skynet, RoboCop, E.T. have real objectives + flags with a proper reveal.

**Independent Test**: Play WOPR to the same `JOSHUA FIVE BY FIVE` reveal (fanfare → flag-intro →
Johnny-5 voice → tally → chess offer); play RoboCop to its flag (reveal in its own voice, tally,
no chess offer).

**Current state**: `conversation()` gates the win on `is_wopr and cfg.enable_flag`, builds the
reveal from `cfg.flag_text`/`cfg.flag`, uses `cfg.flag_voice/speed/pitch`, always plays
`FLAG_INTRO_FRAMES`, and calls `sess.note_game("wopr", won=True)`. The three chat characters have
no `quest`. `flag_tally(sess)` already counts every experience with a `quest`.

- [X] T015 [US2] Generalize the quest-win path in `conversation()` (`wopr_daemon.py`, the block starting `if is_wopr and cfg.enable_flag and not sess.quest.unlocked:`): gate on `sess.experience.quest is not None and cfg.enable_flag`; resolve reveal text as `quest.reveal or cfg.flag_text` with `{flag}` → `quest.flag or cfg.flag`; reveal voice/speed/pitch as `quest.reveal_voice/speed/pitch` falling back to `cfg.flag_*` for WOPR and to the game's own voice otherwise; play the wink only if `quest.fanfare`; play `INTRO_FRAMES[quest.reveal_intro]` if set, else `FLAG_INTRO_FRAMES` only for WOPR; log `event=flag_unlocked key=… flag=…`; call `sess.note_game(sess.experience.key, won=True)`; keep `flag_tally`; keep the chess offer and the `joshua` beat line WOPR-only. Leave `t_first_point_ms`/`t_win_ms` and the per-call top-level `won`/`points`/`beats` sourced from the WOPR quest state so `analyze.py`'s WOPR funnel stays correct
- [X] T016 [P] [US2] Author Skynet's quest in `SKYNET_EXPERIENCE` (`wopr_daemon.py`): tolerant-regex objectives (e.g. `sarah_connor` "Sarah Connor", `john_connor` "John Connor", `ill_be_back` "I'll be back" — accept common STT mishears like "connor/conner/kahn"), `ordered=False`, reveal `"COME WITH ME IF YOU WANT TO LIVE. YOUR FLAG IS {flag}."`, flag `"CYBERDYNE-MODEL-101"` (author-editable), `fanfare=True`, no reveal voice override
- [X] T017 [P] [US2] Author RoboCop's quest in `ROBOCOP_EXPERIENCE` (`wopr_daemon.py`): three prime-directive objectives `serve` / `protect` / `uphold` (`serve the public`, `protect the innocent`, `uphold the law`, tolerant of mishears), `ordered=False`, reveal `"THANK YOU FOR YOUR COOPERATION. YOUR FLAG IS {flag}."`, flag `"PRIME-DIRECTIVE-4-CLASSIFIED"` (author-editable), `fanfare=True`
- [X] T018 [P] [US2] Author E.T.'s quest in `ET_EXPERIENCE` (`wopr_daemon.py`): phone-building objectives (e.g. `speak_and_spell`, `umbrella`, `saw_blade`/`record player`, plus `phone_home`), `ordered=False`, reveal `"E.T. PHONE HOME. YOUR FLAG IS {flag}."`, flag `"REESES-PIECES-1982"` (author-editable), `fanfare=True`
- [X] T019 [US2] Unit tests in `tests/test_dsp.py` (new `QuestWinTests` or extend `GameFrameworkTests`): WOPR still unlocks from the four beats and its reveal text still substitutes `cfg.flag`; each of Skynet/RoboCop/E.T. unlocks from its objectives (canonical phrasing and one near-miss STT spelling each); `flag_tally` reports `n of 4` after one win and "ALL 4" after four; `validate_games` reports 0 invalid on the shipped roster
- [X] T020 [US2] Barge-in regression in `tests/test_dsp.py`: after a whistle mid-quest (simulate `brain_task`'s reset: `sess.experience = WOPR_EXPERIENCE; sess.quest = QuestState(quest=sess.experience.quest)`), quest progress is empty and `sess.games` keeps any previously earned `won` flags for the call (arcade persistence)

**Checkpoint**: Four quests are winnable; WOPR regression proven; `won` recorded per game key.
✅ Done 2026-09-18 (86 tests). Content as shipped: Skynet = I'll be back / hasta la vista / Judgment Day
(any order, flag `CYBERDYNE MODEL ONE ZERO ONE`); RoboCop = the three prime directives **in order**
(flag `DIRECTIVE FOUR CLASSIFIED`); E.T. = Speak & Spell / umbrella / record player (any order, flag
`I WILL BE RIGHT HERE`). Each objective has a canned `response` line spoken on hit (no LLM round trip).
The per-call `won`/`points`/`beats` fields stay the WOPR funnel via `CallSession.wopr_quest`.

---

## Phase 5: US3 — Cutscene plays through (Priority: P1)

**Goal**: Cutscenes play intro → clip → optional message → optional goodbye, mark `completed`, then
either end the call (`end_call=True`) or return to the menu (arcade default).

**Independent Test**: GLaDOS = song → back to menu; Tony Hawk = kickflip clip → pep talk → back to
menu; "goodbye" = sign-off clip → hang up; one teardown, recording + stats written.

- [X] T021 [US3] Implement `run_cutscene` fully in `wopr_daemon.py`: intro (if any) + clip; message per `message_mode` — `none`, `fixed` (TTS `message_text` in the game voice), `generated` (one `llm_reply` seeded by `message_prompt`, fallback to `message_text` or skip on `PipelineError`); optional goodbye; `note_game(completed=True)`; `end_call` only if `cs.end_call`
- [X] T022 [US3] Wire `TONYHAWK_EXPERIENCE.cutscene` (`wopr_daemon.py`): kickflip audio as the intro clip, `message_mode="generated"`, `message_prompt` + fixed fallback pep talk mentioning Patrick Garrity
- [X] T023 [US3] Wire `GLADOS_EXPERIENCE.cutscene` (`wopr_daemon.py`): `clip="song"`, `message_mode="none"`
- [X] T024 [US3] Verify `end_call` sends exactly one terminate frame and is safe on a broken writer (`test_end_call_sends_terminate_frame`, `test_end_call_safe_when_writer_broken` in `tests/test_dsp.py`); teardown remains the single guarded path in `handle_call`
- [X] T025 [P] [US3] Cutscene content tests in `tests/test_dsp.py` (`CutsceneTests`): Tony Hawk has a generated message with a fallback; GLaDOS/Tony Hawk return to the menu while goodbye hangs up; `MENU_RETURN_RE` matches the arcade phrases

**Checkpoint**: ✅ Done (commits `6600f96`, `abac63d`). Spec/contract text is reconciled in Phase 8.

---

## Phase 6: US4 — Analytics: wins vs completions (Priority: P2)

**Goal**: Per-archetype completion signal recorded and reported; backward compatible.

**Independent Test**: After mixed calls, `analyze.py` shows quest **wins** and cutscene
**completions** per game, with no errors on legacy records lacking the fields.

- [X] T026 [US4] `note_game()` in `wopr_daemon.py` accepts `completed=True` and the per-call `games[key]` record carries `completed` (`test_completed_recorded_in_metrics`)
- [X] T027 [US4] `update_stats_db()` in `wopr_daemon.py` increments `completions` beside `wins` per game, `setdefault`-ing it on legacy aggregates (`test_aggregate_counts_completions`)
- [X] T028 [P] [US4] Extend `build_report()` in `analyze.py`: add `"completions": 0` to the per-game counters, increment it with `1 if g.get("completed") else 0`, and add a `completions` column after `wins` in the PER-GAME BREAKDOWN header and row format strings; also relabel the summary line "Won the WOPR flag" so it is clearly the WOPR funnel, not all games
- [X] T029 [P] [US4] Add `AnalyzeReportTests` in `tests/test_dsp.py` that imports `analyze.build_report` and feeds a mixed session list: one legacy record (no `games`, no `completed`), one quest win (`games.wopr.won=True`), one cutscene completion (`games.glados.completed=True`); assert the report renders without error, the `completions` column shows 1 for glados and 0 for wopr, and `wins` shows the inverse

**Checkpoint**: Stats distinguish wins from completions end to end. ✅ Done 2026-09-18.

---

## Phase 7: Polish & Cross-Cutting ⬅️ NEXT

- [X] T041 Add the Pee-wee cutscene game (`PEEWEE_EXPERIENCE` in `wopr_daemon.py`): wake "secret word" / "Pee-wee" / "mecca lecca"; `Cutscene(clips=("peewee-secret-word", "tequila"))` → back to the menu. Cutscene gained a `clips` sequence and the intro dir now loads every `<name>.wav` into `CLIP_FRAMES` as a cutscene clip key
- [ ] T042 Supply the two WAVs on the mini: `<intro_dir>/peewee-secret-word.wav` (the Playhouse scream) and `<intro_dir>/tequila.wav`, 8 kHz-compatible 16-bit PCM like the other clips; confirm `event=clip clip=tequila` at boot and re-record the operator hints clip if it should mention the secret word

- [ ] T030 Run the full `unittest` suite (`./venv/bin/python -m unittest tests.test_dsp`) after US2 and US4 land and fix any regressions
- [ ] T031 Deploy to the mini (scp `wopr_daemon.py` + `analyze.py` to `/opt/wopr`, restart the LaunchDaemon) and confirm a clean boot: `event=games n=6 roster=wopr:quest,skynet:quest,robocop:quest,et:quest,tonyhawk:cutscene,glados:cutscene` and no `game_invalid`
- [ ] T032 Run `harness.py` (stub daemon on 9093) to confirm the 20 ms pacing and detector are unaffected by the framework changes
- [ ] T033 Walk quickstart.md steps 2–7 on the mini (after T040 updates it for arcade mode): WOPR regression, a RoboCop win with tally, GLaDOS/Tony Hawk back to menu, "goodbye" hangs up once, analytics wins vs completions, offline check
- [ ] T034 [P] Add a "Games & the game framework" section to `README.md` (after "Movie-quest flag") describing the two archetypes, arcade mode (menu return, goodbye exit, flag tally), how to add a game with one `Experience(... quest=/cutscene=...)` declaration, the per-quest flags, and the new `completed`/`completions` stats fields; update the analysis-engine section for the new column
- [ ] T035 [P] Final commit and push to `therealcybermattlee/ReadyPhreakerOne` (the stale `run_cutscene` docstring and `Objective` section comment were refreshed with US2)

---

## Phase 8: Reconcile design docs with Arcade mode (new, from code drift)

**Purpose**: The design artifacts describe "cutscene → goodbye → end the call"; the shipped product
is arcade-style. Record the decision so spec, contract, data model, and quickstart match reality
and `/speckit-analyze` does not flag false regressions.

- [ ] T036 [P] Update `specs/002-game-framework/spec.md`: US3 title/narrative, acceptance scenarios 1–3, FR-011, FR-012, SC-004, and the "Ending a cutscene call races with a caller hangup" edge case to say a cutscene finishes with `completed` and then *either* returns to the menu (default) or ends the call when declared; add an arcade-mode paragraph to US1/US2 (menu-return phrases, flags persist per call, spoken tally) and an Assumptions bullet recording the 2026-09-17 decision
- [ ] T037 [P] Update `specs/002-game-framework/contracts/game-definition.md`: add `end_call` and `message_prompt` to the Cutscene block with `goodbye` defaulting to false; make §2 "Cutscene call-end contract" conditional on `end_call=True` and document the arcade return path (game marked `completed`, session drops to ARMED); add a §5 "Arcade mode" listing `MENU_RETURN_RE` phrases and that the `goodbye` experience is the explicit exit
- [ ] T038 [P] Update `specs/002-game-framework/data-model.md`: Cutscene table gains `message_prompt` and `end_call`, `goodbye` default false; Flow line gains the "end the call *or* return to menu" branch; note that `Experience.archetype` also returns `"clip"` for the greeter utility
- [ ] T039 [P] Update `specs/002-game-framework/research.md` with a D7 "Arcade mode" decision (chain games in one call, collect every flag; explicit exit via goodbye; alternatives: one-game-per-call as originally designed) and adjust the D1/D3 summary rows
- [X] T043 The 2600 Hz gate (`wopr_daemon.py`, 2026-09-19): a picked-up line is dead silence until the caller whistles 2600 Hz (`CallSession.seized`, `next_idle_frame`, `on_whistle`); the whistle plays the wink and drops the caller at the menu — every whistle, so WOPR is reached only by saying Joshua; wake phrases are ignored until seized; `seize_watchdog` hangs up an un-whistled line after `--seize-timeout` (90 s, `WOPR_SEIZE_TIMEOUT`, 0 = never); `--no-seize` / `WOPR_NO_SEIZE=1` restores the menu-on-pickup behavior. Per-call metrics gain `seized`
- [ ] T044 [P] Update `specs/002-game-framework/spec.md` FR-016 and US2 acceptance scenario 4 (and spec 001's whistle→WOPR wording where referenced) for the gate: the whistle seizes the trunk / returns to the menu; WOPR is summoned by "Joshua"; add the dead-line-until-2600 behavior and the timeout to FR-015/edge cases
- [ ] T045 Re-record the operator hints clip on the mini to match the new `OPERATOR_HINTS_DEFAULT` (Joshua for WOPR, the new games, "whistle again any time to come back here"); until then the recorded clip still says to whistle for WOPR
- [ ] T040 [P] Update `specs/002-game-framework/quickstart.md` steps 4–5: GLaDOS/Tony Hawk return to the menu; add a "say goodbye → sign-off → hang up" check; add a step for "main menu" mid-quest returning to the menu with flags retained and the tally after a second win

---

## Dependencies & Execution Order

- **Setup (T001–T003)** ✅ → **Foundational (T004–T008)** ✅ → **US1 (T009, T011–T014)** ✅.
- **US2 (T010, T015–T020)** — the next increment. T015 first (generic win path), then T016/T017/T018 in parallel (each edits one `*_EXPERIENCE` block; T010 is satisfied by them), then T019/T020 tests.
- **US3 (T021–T025)** ✅ — independent of US2.
- **US4 (T026–T027)** ✅; **T028/T029** remain and depend only on the stats fields already present, so they can run in parallel with US2 (different files: `analyze.py`, tests).
- **Phase 8 (T036–T040)** — doc-only, no code dependency; can run any time, ideally before T033.
- **Polish (T030–T035)** — after US2 and T028/T029; T031–T033 need the mini.

## Parallel Opportunities

- T016 / T017 / T018 (three experience blocks) after T015.
- T028 (`analyze.py`) alongside any US2 task.
- T036–T040 (five separate docs) alongside everything.
- T034 / T035 after the code settles.
- Tests marked `[P]` all touch `tests/test_dsp.py`; serialize edits to that one file.

## Independent Test Criteria (per story)

- **US1**: add a throwaway game of each archetype → summonable + correct archetype behavior, no shared-loop edits. ✅
- **US2**: WOPR reveals the same flag with the same presentation; RoboCop reveals its flag in its own voice; tally counts both.
- **US3**: GLaDOS/Tony Hawk play and return to the menu with `completed=true`; goodbye hangs up once; recording + stats written. ✅
- **US4**: `analyze.py` separates wins (quests) from completions (cutscenes), no error on legacy data.

## Suggested MVP

Already delivered: **Setup + Foundational + US1 + US3** (parity + cutscenes + arcade). The next
deliverable is **US2** (T015–T020), which turns the three chat characters into winnable quests and
makes the "collect every flag" arcade loop real. Then T028/T029 to finish US4, Phase 8 docs, Polish.
