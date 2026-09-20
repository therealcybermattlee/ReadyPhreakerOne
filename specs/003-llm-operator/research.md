# Phase 0 Research: Conversational Operator

No `NEEDS CLARIFICATION` remained in the Technical Context. The items below are the substantive
design choices, each resolved against the existing daemon.

## D1. Where the Operator lives in the call flow

**Decision**: The Operator is a `chat`-archetype `Experience` (`OPERATOR_EXPERIENCE`, key
`operator`, utility, not a game). `brain_task` launches it (a) right after the seize kerchunk and
(b) whenever a game returns to ARMED (menu return, cutscene end, switch), by setting
`sess.experience = OPERATOR_EXPERIENCE` and running `conversation()` — the same path every chat
character uses. While the Operator conversation runs, the session is not ARMED, so the attract
frames never play; the recorded greeting is played as her opening line instead of TTS.

**Rationale**: Reuses the capture → STT → LLM → TTS loop, the speech barge-in, and the
mid-conversation name switch (saying "RoboCop" to her connects, exactly as the spec wants).
Zero new call-loop machinery.

**Alternatives**: a separate "operator loop" task alongside ARMED — rejected: duplicates the
capture loop and re-creates the sidetone/wake-listener interplay that was hard to get right.

## D2. The directory is generated, not hand-written

**Decision**: Each `Experience` gets `listed: bool = True` and `directory: DirectoryEntry | None`
(`name`, `say` = the spoken names callers can use, `blurb`). `render_directory()` builds the
Operator's system prompt from the listed games only. Utilities (greeter, goodbye) and easter eggs
(`listed=False`, e.g. HAL) are omitted entirely. The rendered text is also what the post-filter
uses to know which names are speakable.

**Rationale**: Adding a game already means adding one `Experience`; its directory entry rides
along, and marking it an easter egg is one flag (FR-009/010, SC-006). The prompt cannot drift from
the roster.

**Alternatives**: a static prompt string — rejected: it would silently go stale as games change
(the operator hints recording already has).

## D3. Keeping an 8B model honest: prompt + post-filter, not trust

**Decision**: The prompt states the closed directory, "answer only from it", and a brush-off
rule, with `num_predict` ≈ 40 and a short reply budget. Then `operator_filter(text)` runs before
TTS: (1) any capitalized/known character token not in the listed set (HAL, Dave, Siri, …) is
removed — a curated blocklist of every non-listed key plus common hallucination names, and a
generic rule for `<Name> 9000`-style constructs; (2) the secret word and its accepted mishears
(the Pee-wee wake regex) are blocked — a violating reply is replaced by a clue-free brush-off;
(3) an emptied reply becomes a fixed line. WOPR-style `postprocess_reply` (caps, no markdown,
sentence truncation) applies first.

**Rationale**: FR-008/011 and SC-002/003 are hard guarantees; a prompt alone cannot give them
with a small local model. The filter is cheap, deterministic, and unit-testable.

**Alternatives**: a second "judge" LLM call — rejected: doubles latency for a marginal gain.

## D4. Secret-word clues are an ordered list with per-call progress

**Decision**: `OPERATOR_CLUES` is an ordered tuple of author-written clue lines. A regex
(`SECRET_WORD_ASK_RE`: "secret word", "password", "what's the word") intercepts the caller's
utterance *before* the LLM: the daemon speaks the next clue (`sess.clue_idx += 1`, wrapping to a
"that is all I have" line) with no model call. Everything else goes to the model, which is told
only that a secret word exists and to refer clue-seekers back to her.

**Rationale**: Deterministic, ordered, never leaks (FR-007, SC-003), and it is the fastest turn
on the phone (STT + TTS only). Mirrors the objective-response pattern the quests use.

**Alternatives**: let the model give clues — rejected: order and leak-proofing are not
guaranteeable.

## D5. Nudges: a capture timeout in the loop

**Decision**: `conversation()` gains an optional per-experience `idle_nudge_s` (Operator: 12).
In the capture loop, `utterance_evt.wait()` runs under `asyncio.wait_for(timeout)`; on timeout
the Operator speaks one of a few short nudge lines (rotating) and capture resumes. The timer
counts from the end of her last speech (capture starts after playback) and any caller speech
cancels it because the utterance arrives before the timeout.

**Rationale**: Replaces the menu's "repeat every `menu_gap_ms`" behavior with the same cadence
(FR-013, SC-007) using one `wait_for`. Games keep `idle_nudge_s = None` (no change).

**Alternatives**: a background nudge task — rejected: races the capture state machine.

## D6. Degraded mode: the recorded menu loop stays

**Decision**: `Config.enable_operator` (`--no-operator` / `WOPR_OPERATOR=0`) and a runtime check
`operator_available()` = `not cfg.stub_llm and llm_warm_ok` (the existing warmup task records
whether Ollama answered). If false, `brain_task` sets ARMED as today and the attract/menu loop
plays; wake phrases work unchanged. If the Operator is live and one `llm_reply` fails or exceeds
the reply budget (`asyncio.wait_for(..., OPERATOR_REPLY_BUDGET_S=5)`), she speaks a fixed
fallback line and stays in the conversation.

**Rationale**: FR-015/016, SC-005. The menu loop code is not deleted; it becomes the fallback.

**Alternatives**: TTS-only scripted operator as the fallback — rejected: the recorded loop
already exists and is proven.

## D7. The deterministic "list the options" path

**Decision**: `LIST_OPTIONS_RE` ("list the options", "what are my options", "operator") spoken
to the Operator plays the recorded hints clip (`OPERATOR_FRAMES`) instead of a model reply, then
capture resumes. Note the recorded clip predates the easter-egg rule and lists only listed games;
if a future easter egg is added to that recording it must be re-recorded.

**Rationale**: FR-019; gives callers a guaranteed complete answer without trusting the model.

## D8. Voice and opening line

**Decision**: The Operator uses a distinct espeak voice (`en-us+f3`, slightly faster) so she is
audibly not WOPR; overridable via `WOPR_OPERATOR_VOICE/SPEED/PITCH`. Her opening line is the
recorded menu greeting (`ATTRACT_FRAMES` first repeat without the pad; or TTS `menu_greeting` if
no recording). The prompt tells her not to repeat the greeting verbatim.

**Rationale**: FR-002; the recorded greeting is the owner's voice and already says "welcome to
GrrCon, how may I help you".

## Summary of decisions

| # | Decision |
|---|---|
| D1 | Operator = chat `Experience`; `brain_task` runs it after the seize and on every return to the menu |
| D2 | Directory rendered from `WAKE_EXPERIENCES` via `listed` + `directory` fields; easter eggs omitted |
| D3 | Prompt + deterministic post-filter (unknown names, secret word, empty → fixed line) |
| D4 | Ordered clue list served by regex intercept, per-call index; no model involvement |
| D5 | Nudge = capture timeout (`idle_nudge_s = 12`), rotating lines |
| D6 | `--no-operator` / Ollama-down → today's recorded menu loop; per-reply budget with fixed fallback |
| D7 | "List the options" plays the recorded hints clip |
| D8 | Distinct voice; recorded greeting is the opening line |
