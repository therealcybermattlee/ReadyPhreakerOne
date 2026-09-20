# Feature Specification: Conversational Operator

**Feature Branch**: `003-llm-operator`

**Created**: 2026-09-20

**Status**: Draft

**Input**: User description: "A conversational Operator for the ReadyPhreakerOne payphone that replaces the looping recorded menu. After the caller seizes the trunk with the 2600 Hz whistle (and whenever they return to the menu), they reach a switchboard Operator character driven by the local LLM, in the phreaking tradition of social-engineering the operator for the directory. The Operator's knowledge is a small, closed corpus: the playable characters and how to reach each one, that a secret word exists (revealed only as riddles/clues, never the word itself), 'whistle again to come back to me', and 'say goodbye to hang up'. She reveals only what is in the directory, one clue at a time, and gives a switchboard-style brush-off for anything else; short replies. Saying a character's name to her connects the caller straight to that game. 'Main menu' from inside a game returns to her. If the caller is silent for ~12 seconds she offers a nudge. Any character name she speaks must be one of the real characters. Her turns are not counted as game play in analytics. If the LLM is unavailable, the recorded menu loop is used instead. The existing recorded menu greeting remains her opening line. No hardware, Asterisk, whistle-detection, recording or stats changes." Follow-up from the owner: some characters (e.g. HAL) are **easter eggs** — reachable by their wake phrase but never listed or hinted by the Operator.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Caller reaches a live Operator after the whistle (Priority: P1)

A caller picks up, hears nothing, whistles 2600 Hz, hears the trunk seize, and is greeted by a switchboard Operator: the existing recorded greeting line, then a live back-and-forth. The caller can ask her what is available, who is on the line, or how to reach someone, and she answers in character from her directory, one short reply at a time. There is no looping menu; the Operator is the menu.

**Why this priority**: This is the feature. It turns the dead-end recorded loop into the phreaking fantasy of talking your way past the operator, and every other story depends on her existing.

**Independent Test**: Whistle in, ask "what can I do here?", "who else is on this line?", "how do I reach the war computer?", and confirm each answer is short, in character, and drawn only from the directory.

**Acceptance Scenarios**:

1. **Given** the trunk was just seized, **When** the kerchunk finishes, **Then** the caller hears the recorded greeting line and the Operator is listening; no menu loop plays.
2. **Given** the Operator is listening, **When** the caller asks what they can do, **Then** she offers directory information in one or two short sentences and stops.
3. **Given** the Operator is listening, **When** the caller asks about something not in her directory (the weather, her name, the meaning of life), **Then** she gives a brief switchboard-style brush-off and offers to connect them to someone.
4. **Given** the Operator is listening, **When** the caller says a listed character's name, **Then** the call connects to that character exactly as a wake phrase does today.

---

### User Story 2 - Clues, not answers, for the secret word (Priority: P1)

The caller has heard there is a secret word. They try to get it from the Operator. She confirms one exists, gives one clue per request, escalating in helpfulness, and never says the word itself. Working it out and saying it triggers the secret-word character as today.

**Why this priority**: The secret word is the puzzle that makes the Operator worth social-engineering. Without gating, she is a menu with extra steps.

**Independent Test**: Ask for the secret word three times in a row and confirm three distinct clues, none containing the word; then say the word and confirm the connection.

**Acceptance Scenarios**:

1. **Given** the caller asks for the secret word, **When** it is the first ask this call, **Then** she confirms one exists and gives the first clue.
2. **Given** the caller has already had a clue, **When** they ask again, **Then** she gives the next clue, not the same one.
3. **Given** any number of asks, **When** she replies, **Then** the reply never contains the secret word.
4. **Given** the caller says the secret word to her, **When** it is recognized, **Then** the secret-word character plays as it does from the menu today.

---

### User Story 3 - Easter eggs stay hidden (Priority: P1)

Some characters are marked as easter eggs. They are reachable by their wake phrase from anywhere, but the Operator never lists them, hints at them, or confirms they exist. If asked directly, she denies knowledge in character.

**Why this priority**: The owner wants a class of rewards that only lore knowledge unlocks. An Operator that reads out every extension would defeat it.

**Independent Test**: Ask the Operator "who is on this line?" and "is HAL here?" and confirm HAL is never mentioned or confirmed; then say "open the pod bay doors" and confirm HAL plays.

**Acceptance Scenarios**:

1. **Given** an easter-egg character exists, **When** the caller asks for the directory, **Then** it is not listed.
2. **Given** an easter-egg character exists, **When** the caller asks about it by name, **Then** she denies any such extension in character.
3. **Given** an easter-egg character exists, **When** the caller says its wake phrase, **Then** it plays as it does today.

---

### User Story 4 - Returning to the Operator and nudges (Priority: P2)

From inside any game the caller says "main menu" and is back with the Operator, who acknowledges them briefly. If the caller goes quiet for about twelve seconds while with the Operator, she offers a short nudge. Saying "goodbye" to her hangs up as today; whistling again also returns to her.

**Why this priority**: These are the connective tissue that keeps a caller from getting stranded, replacing the repeat-every-ten-seconds behavior of the old loop.

**Independent Test**: Enter a game, say "main menu", confirm the Operator answers; stay silent, confirm a nudge arrives in roughly twelve seconds and does not repeat more often than that.

**Acceptance Scenarios**:

1. **Given** the caller is in a game, **When** they say "main menu", **Then** the Operator greets them back within a couple of seconds.
2. **Given** the caller is with the Operator, **When** they say nothing for ~12 seconds, **Then** she speaks one short nudge; **When** they stay silent, **Then** nudges recur no more often than every ~12 seconds.
3. **Given** the caller is with the Operator, **When** they say "goodbye", **Then** the sign-off plays and the call ends as today.

---

### User Story 5 - Degraded mode without the language model (Priority: P2)

If the language model is unavailable, the phone falls back to today's recorded menu loop after the whistle, so callers still reach every game by wake phrase. Nothing else about the call changes.

**Why this priority**: The installation runs offline for a weekend; the phone must never go dead because one component did.

**Independent Test**: Stop the language model, whistle in, confirm the recorded menu loop plays and wake phrases still connect games.

**Acceptance Scenarios**:

1. **Given** the language model is down or disabled, **When** the trunk is seized, **Then** the recorded menu loop plays and wake phrases work as today.
2. **Given** the Operator is live and a single reply fails to generate, **When** that happens, **Then** she falls back to a short fixed line and stays available; the call does not hang.

---

### Edge Cases

- **The Operator invents an extension**: any character name she is about to speak that is not a real, non-easter-egg character must be removed or replaced before it is spoken; if the reply is then empty, a fixed line is used.
- **The Operator says the secret word**: replies are checked for the word (and close spellings) before speech; a violating reply is replaced with a clue-free brush-off.
- **Caller says a wake phrase inside a question** ("is RoboCop here?"): connecting immediately is acceptable and expected; the Operator does not need to answer first.
- **Caller interrupts the Operator**: the existing talk-over-a-clip behavior applies; her reply stops and their words are taken as the next utterance.
- **Recorded greeting and the Operator's first reply overlap in meaning**: the greeting is her opening line; she must not repeat it verbatim as her first generated reply.
- **Silence nudge while the caller is mid-sentence**: the nudge timer only counts from the end of her last reply and is cancelled the moment the caller starts speaking.
- **Language model is slow**: a reply that takes longer than a few seconds is abandoned for a fixed line so the caller is never left in silence.

## Requirements *(mandatory)*

### Functional Requirements

**The Operator**

- **FR-001**: After the trunk is seized, and whenever the caller returns to the menu, the system MUST place the caller in a conversation with the Operator instead of the recorded menu loop.
- **FR-002**: The Operator's opening line MUST be the existing recorded menu greeting.
- **FR-003**: The Operator MUST answer from a closed directory only: the listed characters and how to reach each, the existence of a secret word (as clues), "whistle again to return to the Operator", and "say goodbye to hang up".
- **FR-004**: Replies MUST be short (a sentence or two) and in the character of a switchboard operator; the Operator MUST NOT break character or describe herself as software.
- **FR-005**: For anything outside the directory the Operator MUST give a brief in-character brush-off and steer back to the directory.
- **FR-006**: Saying a listed character's name to the Operator MUST connect the caller to that character exactly as a wake phrase does today.

**Secret word**

- **FR-007**: The Operator MUST confirm a secret word exists and give one clue per request, progressing through an ordered clue list within a call.
- **FR-008**: The Operator MUST never speak the secret word or a close spelling of it; replies MUST be checked before speech and replaced if they violate this.

**Easter eggs**

- **FR-009**: Every character MUST carry a directory classification: **listed** or **easter egg**. Easter-egg characters MUST NOT appear in any directory reply, clue, or confirmation from the Operator; asked directly, she MUST deny in character.
- **FR-010**: Easter-egg characters MUST remain reachable by their wake phrases from the Operator and from inside games, unchanged.
- **FR-011**: Any character name in a reply that is not a listed character MUST be removed before speech; an emptied reply falls back to a fixed line.

**Flow**

- **FR-012**: "Main menu" from inside a game MUST return the caller to the Operator, who acknowledges briefly.
- **FR-013**: When the caller is silent with the Operator for ~12 seconds (measured from the end of her last speech), she MUST offer one short nudge, and MUST NOT nudge more often than that interval.
- **FR-014**: "Goodbye" to the Operator MUST play the sign-off and end the call as today; a whistle MUST still return the caller to the Operator.

**Resilience**

- **FR-015**: If the language model is unavailable or disabled, the system MUST use the existing recorded menu loop after the whistle so that all games remain reachable by wake phrase.
- **FR-016**: If a single Operator reply cannot be produced in time, the system MUST speak a fixed fallback line and remain in the Operator conversation.

**Preserved behavior**

- **FR-017**: The Operator MUST NOT be counted as a game in per-call or aggregate analytics; her turns are not game turns.
- **FR-018**: Whistle detection, the 2600 gate, recording, per-game analytics, and hardware/telephony configuration MUST NOT change.
- **FR-019**: The existing "operator" hints recording MUST remain available on request (e.g. "list the options") as a deterministic answer.

### Key Entities *(include if feature involves data)*

- **Operator**: the switchboard host character; has an opening line (recorded), a persona, a directory, a clue list, a nudge line, and fixed fallback lines.
- **Directory entry**: one listed character — a display name, how to reach it (its spoken names), and a one-line description the Operator may use.
- **Directory classification**: per character, listed or easter egg.
- **Clue list**: the ordered secret-word clues; per-call progress through it.
- **Call state (Operator)**: which clue is next, time of her last speech (for nudges).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A first-time caller who asks the Operator how to reach any listed character gets a usable answer within one exchange, and the connection works when they say the name.
- **SC-002**: Across 20 scripted questions outside the directory, the Operator never states a fact that is not in the directory and never names a non-existent or easter-egg extension.
- **SC-003**: Across 20 requests for the secret word, the word is never spoken, and at least three distinct clues are given in order.
- **SC-004**: Each Operator reply begins within 3 seconds of the caller finishing speaking, in the normal (online, warm) state.
- **SC-005**: With the language model stopped, 100% of games remain reachable after the whistle via the recorded menu.
- **SC-006**: Easter-egg characters are never mentioned by the Operator across the scripted question set, and each still plays from its wake phrase.
- **SC-007**: A caller left silent hears a nudge after roughly 12 seconds and not more than one nudge per 12 seconds.

## Assumptions

- The local language model, speech-to-text, and text-to-speech already used by the games are the substrate; the Operator adds a persona and a directory, not new services.
- "Short" means the same reply budget the game characters use today (one or two sentences).
- The directory is authored as content alongside the game definitions, so adding a game adds its directory entry (or marks it an easter egg) in the same place.
- Clue wording for the secret word is authored content; three or more clues are expected.
- The recorded "operator" hints clip and recorded menu greeting stay as they are; the greeting doubles as her opening line.
- HAL is the first easter egg; GLaDOS, Tony Hawk, and Pee-wee's secret word remain listed unless the owner reclassifies them.

## Out of Scope

- Changing the games, their quests, or the arcade flow.
- Voice cloning or a new voice engine for the Operator; she uses the existing synthesizer with a distinct voice setting.
- Persisting Operator conversations across calls.
- Hardware, ATA, Asterisk, or whistle-detection changes.
