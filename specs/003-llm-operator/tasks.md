---
description: "Task list for the Conversational Operator (spec 003)"
---

# Tasks: Conversational Operator

**Input**: Design documents from `/specs/003-llm-operator/` (spec, plan, research D1–D8, data-model, contracts/operator.md, quickstart)

**Tests**: Included (`tests/test_dsp.py`, `OperatorTests`), pure logic with no LLM/TTS.

**Status 2026-09-20**: implemented in one pass on `main` right after planning (the owner asked to build it the same day). Tasks are recorded as executed; remaining items are the on-phone validation and content polish.

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [X] T001 Add `DirectoryEntry` dataclass and `listed` / `directory` / `aliases` / `idle_nudge_s` fields on `Experience` in `wopr_daemon.py`
- [X] T002 Add `MENU_GREETING_FRAMES` (the recorded greeting without the loop pad) and `LLM_WARM_OK` (set by `llm_warmup_task`) in `wopr_daemon.py`
- [X] T003 Add `Config.enable_operator`, `operator_voice/speed/pitch`, `operator_reply_budget_s` and the `--no-operator` / `--operator-*` args (`WOPR_OPERATOR=0`, `WOPR_OPERATOR_VOICE`, …) in `wopr_daemon.py`

## Phase 2: Foundational

- [X] T004 Directory entries on every listed game (WOPR, Skynet, RoboCop, E.T., Tony Hawk, GLaDOS, the secret word) and `listed=False` + `aliases` on HAL in `wopr_daemon.py`
- [X] T005 `listed_games()`, `render_directory()`, `build_operator()` → `OPERATOR_EXPERIENCE` (chat archetype, distinct voice, `idle_nudge_s=12`) in `wopr_daemon.py`
- [X] T006 `validate_games`: listed game without a directory entry, easter egg with one, and any `say` that would not summon its game are logged `game_invalid` in `wopr_daemon.py`
- [X] T007 `operator_filter(text)`: strip easter-egg aliases, replace a reply containing the secret word (Pee-wee wake regex) with the brush-off, fixed line if emptied in `wopr_daemon.py`
- [X] T008 `operator_available(cfg)` = enabled and not stub/no-stt and the warmup succeeded; `CallSession.route_to_operator()` in `wopr_daemon.py`

## Phase 3: US1 — Caller reaches a live Operator after the whistle (P1)

- [X] T009 [US1] `brain_task`: after the seize kerchunk and after every conversation that returns without a pending switch, `route_to_operator()` (else stay ARMED: the recorded menu loop) in `wopr_daemon.py`
- [X] T010 [US1] `conversation()`: Operator opening = the recorded greeting once per call (`sess.greeted`), later returns speak `OPERATOR_RETURN_LINE`; no `note_game` for her in `wopr_daemon.py`
- [X] T011 [US1] Operator turns: budgeted `llm_reply` (`num_predict=40`, `asyncio.wait_for` on `operator_reply_budget_s`) → `operator_filter` → TTS in the Operator voice; recorded as `kind=operator`, not counted as game turns in `wopr_daemon.py`
- [X] T012 [US1] Saying a listed name to her connects via the existing mid-conversation switch (no change needed; covered by `test_every_directory_say_summons_its_game_and_roster_validates`)

## Phase 4: US2 — Clues, not answers (P1)

- [X] T013 [US2] `OPERATOR_CLUES` (3, ordered), `OPERATOR_CLUES_EXHAUSTED`, `SECRET_WORD_ASK_RE`; clue intercept before the model with per-call `sess.clue_idx` in `wopr_daemon.py`
- [X] T014 [US2] Test: no authored Operator line (clues, nudges, fallback, brush-off) matches the secret-word regex; filter blocks "GRRCON"/"GRR CON" in `tests/test_dsp.py`

## Phase 5: US3 — Easter eggs stay hidden (P1)

- [X] T015 [US3] HAL `listed=False` with aliases; directory omits him; filter strips "HAL 9000"/"HAL"/"DAVE" (`test_filter_strips_easter_egg_names_and_blocks_the_secret_word`, `test_directory_lists_only_listed_games`)

## Phase 6: US4 — Returning to the Operator and nudges (P2)

- [X] T016 [US4] "main menu" said to the Operator → `OPERATOR_MENU_ACK` (stays with her); from a game → existing menu return → `route_to_operator()` in `wopr_daemon.py`
- [X] T017 [US4] Capture timeout = `experience.idle_nudge_s` → rotating `OPERATOR_NUDGES` (`sess.nudge_idx`), then listen again; games unchanged (`None`) in `wopr_daemon.py`
- [X] T018 [US4] "list the options" / "who is on the line" → the recorded hints clip (`LIST_OPTIONS_RE`) in `wopr_daemon.py`

## Phase 7: US5 — Degraded mode (P2)

- [X] T019 [US5] `--no-operator`, `--stub-llm`, `--no-stt`, or no successful warmup ⇒ `operator_available()` false ⇒ the recorded menu loop hosts (log `event=operator_unavailable`) in `wopr_daemon.py`
- [X] T020 [US5] Per-reply timeout/error ⇒ `OPERATOR_FALLBACK` (log `event=operator_fallback reason=…`), call stays live in `wopr_daemon.py`

## Phase 8: Polish & validation

- [X] T021 Whistle-once regression: keep feeding the tone detector while `sess.seizing` so the seize sequence sees the whistle end (it was waiting the full 3 s) in `wopr_daemon.py`
- [ ] T022 Walk quickstart.md steps 2–8 on the mini: greeting once, directory answers, brush-off, three clues then exhausted, HAL denied, connect/return/nudge/exit, degraded mode, latency
- [ ] T023 Tune `OPERATOR_PERSONA` / clue wording from the first real calls; consider `WOPR_OPERATOR_VOICE` alternatives (`en-us+f3` default)
- [ ] T024 [P] Update `README.md` (Operator section: directory entries, easter eggs, `--no-operator`, env vars) and `deploy/wopr.env.example`
- [ ] T025 [P] Re-record `/opt/wopr/operator.wav` hints: drop "whistle again", say "main menu"; it remains the "list the options" answer

## Dependencies

Setup → Foundational → US1 → (US2, US3, US4, US5 independent) → Polish. All code phases are complete; T022 is the gate before the event.
