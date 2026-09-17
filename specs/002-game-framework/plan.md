# Implementation Plan: Game Framework

**Branch**: `002-game-framework` | **Date**: 2026-09-16 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-game-framework/spec.md`

## Summary

Restructure the ad-hoc character handling in the ReadyPhreakerOne daemon into a declarative
**Game** framework with two archetypes. A **Quest** game is an interactive, multi-turn conversation
with tolerant objective matchers, an ordered/any-order requirement, and a win that reveals a flag
(WOPR's existing `MovieQuest` becomes one configured instance). A **Cutscene** game is
non-interactive: it plays a clip, optionally speaks one message, plays the goodbye sign-off, then
**ends the call** by terminating the AudioSocket so the payphone hangs up. Games are declared as
data attached to each `Experience`; the shared call loop dispatches on archetype. The existing
per-character intro clips, per-call recording, and analytics are preserved and gain a per-archetype
completion signal (`won` for quests, `completed` for cutscenes).

## Technical Context

**Language/Version**: Python 3.14 (Homebrew, on the mini and the dev box)

**Primary Dependencies**: stdlib `asyncio`; `numpy`; `faster-whisper` (STT, base.en int8);
Ollama `/api/chat` (qwen3:8b, local loopback); `espeak-ng` (TTS); Asterisk 22 AudioSocket. No new
runtime dependency is introduced by this feature.

**Storage**: append-only JSONL (`sessions.jsonl` per-call metrics, `dataset.jsonl` turn transcript),
a JSON aggregate DB (`game_stats.json`), and WAV clip files under `/opt/wopr` (intros, song,
operator/goodbye clips). No database engine.

**Testing**: `unittest` (`tests/test_dsp.py`), plus the phone-free `harness.py` for the audio path.

**Target Platform**: headless Apple Silicon Mac mini (macOS), fully offline at call time.

**Project Type**: single-file `asyncio` daemon (`wopr_daemon.py`) behind Asterisk; effectively a
CLI/service. Single project.

**Performance Goals**: the 20 ms outbound pacing loop must never stall; STT ~0.3 s and LLM ~1.7 s
per turn are acceptable; a cutscene must reach hang-up promptly after its goodbye.

**Constraints**: offline at call time; keep the daemon a single file with no new heavy deps; do not
regress WOPR; ending a call must not break teardown, recording, or stats; author-editable content.

**Scale/Scope**: an art installation — one phone, sequential callers, a handful of games.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The project constitution (`.specify/memory/constitution.md`) is the unfilled template, so there are
no ratified formal principles. In their place, the project's established conventions are used as
gates:

| Gate | Requirement | This plan |
|---|---|---|
| Offline-first | No internet at call time | PASS — no new external calls; all local |
| Single-file daemon | Logic stays in `wopr_daemon.py`, no new heavy deps | PASS — a Game dataclass + dispatch, reusing existing pipeline |
| Tests + harness | `unittest` green; audio harness unaffected | PASS — pure logic (matchers, dispatch, stats) is unit-tested; audio path reused |
| No WOPR regression | WOPR quest/flag/chess/whistle identical | PASS — WOPR is expressed as a Game with the same beats/flag |
| Deploy unchanged | scp + LaunchDaemon restart; `/opt/wopr` layout | PASS — same deploy flow; new clips reuse the existing clip loader |

No violations; Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/002-game-framework/
├── plan.md              # This file
├── research.md          # Phase 0 output — key design decisions
├── data-model.md        # Phase 1 output — Game / Objective / Quest / Cutscene entities
├── quickstart.md        # Phase 1 output — how to validate end to end
├── contracts/
│   └── game-definition.md   # The declarative Game definition contract + call-end contract
├── checklists/
│   └── requirements.md  # Spec quality checklist (from /speckit-specify)
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
wopr_daemon.py           # the daemon — Game framework added here:
                         #   - Experience/Game definitions (Quest + Cutscene archetypes)
                         #   - generalized Quest engine (from MovieQuest)
                         #   - conversation() dispatch on archetype
                         #   - clean call-end for cutscenes (AudioSocket terminate)
analyze.py               # per-game breakdown already renders; extend for quest-vs-cutscene
tests/test_dsp.py        # unit tests: objective matching, quest win/order, cutscene flow, stats
harness.py               # audio-path harness (unchanged; used to validate no pacing regression)
deploy/asterisk/…        # unchanged (out of scope)
specs/001-whistle-wopr-daemon/contracts/audiosocket-protocol.md  # referenced for the terminate frame
```

**Structure Decision**: Single project. The framework lives inside the existing single-file daemon
(`wopr_daemon.py`) as a small set of declarative dataclasses plus a dispatch on archetype, reusing
the current STT/LLM/TTS/recording/stats machinery. No new modules, services, or dependencies.

## Complexity Tracking

> No constitution violations; nothing to justify.
