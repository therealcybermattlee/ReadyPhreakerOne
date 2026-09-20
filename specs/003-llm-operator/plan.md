# Implementation Plan: Conversational Operator

**Branch**: `003-llm-operator` (work lands on `main`; the owner keeps a single branch) | **Date**: 2026-09-20 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/003-llm-operator/spec.md`

## Summary

Replace the looping recorded menu that follows the 2600 Hz trunk seize with a **switchboard
Operator**: a chat-archetype `Experience` driven by the local LLM whose knowledge is a closed
**directory** generated from the game definitions. Each `Experience` gains a directory entry and a
`listed` / `easter_egg` classification; the Operator's system prompt is rendered from the listed
entries plus an ordered **clue list** for the secret word. Her replies pass a **post-filter**
(strip non-listed character names, block the secret word) before speech. Silence is handled by a
**capture timeout** that speaks a nudge every ~12 s. If Ollama is unreachable or `--stub-llm` is
set, the daemon keeps today's recorded **menu loop** so every game stays reachable. Reuses
`conversation()`, `llm_reply`, `tts_frames`, the mid-conversation name switch, and the speech
barge-in added in the 002 test-call fixes. No new dependencies; single file.

## Technical Context

**Language/Version**: Python 3.14 (Homebrew, mini and dev box)

**Primary Dependencies**: stdlib `asyncio`; `numpy`; `faster-whisper` (STT); Ollama `/api/chat`
(qwen3:8b, loopback); `espeak-ng` (TTS); Asterisk 22 AudioSocket. No new runtime dependency.

**Storage**: none new. Per-call JSONL/aggregate stats unchanged; Operator turns are excluded from
game stats (she is a utility like the greeter).

**Testing**: `unittest` (`tests/test_dsp.py`): directory rendering, classification, post-filter,
clue progression, nudge timing (pure logic, stubs for LLM/TTS). Phone-free `harness.py` for the
audio path. Manual quickstart on the mini.

**Target Platform**: headless Apple Silicon Mac mini, offline at call time.

**Project Type**: single-file `asyncio` daemon (`wopr_daemon.py`). Single project.

**Performance Goals**: first Operator audio within ~3 s of the caller finishing (STT ~0.3 s +
LLM ≤ 2 s at `num_predict` ≈ 40 + TTS ~0.1 s); a reply exceeding a hard budget (5 s) is abandoned
for a fixed line. The 20 ms outbound pacing loop must never stall.

**Constraints**: offline at call time; no new heavy deps; she must never invent an extension,
name an easter egg, or say the secret word; the recorded greeting stays her opening line; the
2600 gate, whistle detection, recording, per-game analytics, telephony config unchanged.

**Scale/Scope**: one phone, sequential callers, eight games (one easter egg today: HAL).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The constitution (`.specify/memory/constitution.md`) is still the unfilled template; the
project's established conventions serve as gates:

| Gate | Requirement | This plan |
|---|---|---|
| Offline-first | No internet at call time | PASS — same local Ollama/Whisper/espeak; degraded mode without Ollama |
| Single-file daemon | Logic stays in `wopr_daemon.py`, no new heavy deps | PASS — one Experience + directory renderer + post-filter + timeout |
| Tests + harness | `unittest` green; audio harness unaffected | PASS — pure logic unit-tested with stubs; audio path reused |
| No WOPR regression | Games, quests, flags, arcade flow identical | PASS — games untouched; the Operator only replaces the ARMED menu |
| Never dead | Phone works when a component fails | PASS — recorded menu loop fallback; fixed-line fallback per reply |
| Deploy unchanged | scp + LaunchDaemon restart; `/opt/wopr` layout | PASS — same files; new env/flags are optional |

No violations; Complexity Tracking is empty. Re-checked after Phase 1: still passes.

## Project Structure

### Documentation (this feature)

```text
specs/003-llm-operator/
├── plan.md              # This file
├── research.md          # Phase 0 — design decisions D1–D8
├── data-model.md        # Phase 1 — Directory entry, Operator, clue list, per-call state
├── quickstart.md        # Phase 1 — how to validate end to end
├── contracts/
│   └── operator.md      # The directory/classification authoring contract + reply guarantees
├── checklists/
│   └── requirements.md  # Spec quality checklist (from /speckit-specify)
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
wopr_daemon.py           # everything:
                         #   - Experience gains `listed: bool` and `directory: DirectoryEntry | None`
                         #   - OPERATOR_EXPERIENCE (chat archetype, distinct espeak voice)
                         #   - render_directory() -> the Operator's system prompt from WAKE_EXPERIENCES
                         #   - OPERATOR_CLUES (ordered) + per-call clue index on CallSession
                         #   - operator_filter(text) -> strips non-listed names / secret word
                         #   - brain_task: after the seize kerchunk (and on menu return) -> Operator
                         #     conversation when LLM is available, else the recorded menu loop (today)
                         #   - conversation(): capture timeout -> nudge line (Operator only)
                         #   - "list the options" -> the recorded operator hints clip (deterministic)
analyze.py               # unchanged (Operator is not a game; not in GAME_LABEL)
tests/test_dsp.py        # OperatorTests: directory rendering, easter-egg exclusion, post-filter,
                         #   clue progression, nudge timing, fallback selection
harness.py               # unchanged
deploy/wopr.env.example  # documents WOPR_OPERATOR=0 (force the recorded menu) and voice overrides
```

**Structure Decision**: Single project, single file. The Operator is one more `Experience` plus
a directory renderer and a reply filter; the dispatch point is `brain_task`'s ARMED handling.

## Complexity Tracking

> No constitution violations; nothing to justify.
