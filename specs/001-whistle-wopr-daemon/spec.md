# Feature Specification: 2600 Hz Whistle → WOPR AudioSocket Daemon

**Feature Branch**: `001-whistle-wopr-daemon`

**Created**: 2026-09-04

**Status**: Draft

**Updated**: 2026-09-04 (deployment constraints: local LLM, Mac mini, Asterisk provisioning in scope)

**Input**: User description: "Build: AudioSocket daemon — 2600 Hz whistle → WOPR. A physical coin-op payphone wired through an FXS ATA to Asterisk hands calls to a Python daemon over AudioSocket. When the caller whistles 2600 Hz into the handset, the daemon plays a wink tone and opens a voice conversation with an LLM playing WOPR from WarGames. Phone-phreaking homage; period aesthetic beats modern fidelity." Amended 2026-09-04: "This will be running on a local llm on a mac mini, and local asterix on same mac mini, anything we need must live on this asset, we will ssh into the asset to do the work."

## Overview

A caller picks up a real coin-op payphone and hears nothing but line silence. If they whistle a
2600 Hz tone into the handset (the classic blue-box "seize trunk" tone), the line answers with a
short wink, and a flat, all-caps computer voice greets them as "Professor Falken." From that point
they can hold a spoken conversation with WOPR, the WarGames supercomputer, which keeps steering the
conversation toward playing a game. Whistling 2600 Hz again at any point cuts WOPR off and starts a
fresh session. Hanging up ends everything.

The tone is a **human acoustic whistle** through a payphone handset, not injected audio. Detection
must tolerate pitch drift, breathiness, and amplitude wobble. Speech, background noise, and the
daemon's own audio bleeding back through the handset sidetone must **not** trigger it.

**Everything runs on one Mac mini.** The phone switch (Asterisk), the whistle daemon, the
speech recognizer, the speech synthesizer, and the language model all live on a single Apple
Silicon Mac mini on the same LAN as the payphone's ATA. Nothing in the call path leaves that
machine. There is no cloud dependency of any kind at call time. The machine is headless and is
administered only over SSH, so every component must start on boot without a logged-in desktop
session. Asterisk is **not yet installed** on the mini; installing and configuring it is part of
this feature.

## Clarifications

### Session 2026-09-04

- Q: Which local LLM runtime should WOPR use on the Mac mini? → A: **Ollama** (replaces the
  original Anthropic API / `claude-sonnet-4-6` mandate; no cloud LLM at all).
- Q: What is the state of Asterisk on the Mac mini today? → A: **Not installed.** Installing and
  configuring Asterisk with AudioSocket support, and registering the HT802 to it, is in scope.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Whistle detection verified without a phone (Priority: P1)

A developer, with no payphone or Asterisk box at hand, runs a test harness that synthesizes a set
of audio clips and replays them into the daemon exactly as Asterisk would. The harness reports
whether each clip triggered detection and how long it took, so detector parameters can be tuned
and false-positive behaviour can be measured before any hardware is touched.

**Why this priority**: The whole feature hinges on reliable, tunable detection of an imperfect
human whistle. Nothing downstream is meaningful if the detector fires on speech or misses a real
whistle. The user explicitly asked for this to be built first.

**Independent Test**: Start the daemon on a local port, run the harness against it with the
synthesized clip set, and read the pass/fail table. Delivers value on its own: a validated,
tuned detector with a regression suite.

**Acceptance Scenarios**:

1. **Given** the daemon is listening, **When** the harness replays a clean, steady 2600 Hz tone of at least 400 ms, **Then** the daemon reports a detection event within 500 ms of tone onset.
2. **Given** the daemon is listening, **When** the harness replays a whistle-like tone whose pitch wanders ±30 Hz around 2600 Hz with amplitude wobble and breath noise, **Then** the daemon still reports a detection event.
3. **Given** the daemon is listening, **When** the harness replays a clip of spoken speech containing no sustained 2600 Hz component, **Then** no detection event is reported for the entire clip.
4. **Given** the daemon is listening, **When** the harness replays speech that contains sustained energy near but not at 2600 Hz (e.g. 2500 Hz or 2700 Hz harmonics), **Then** no detection event is reported.
5. **Given** the daemon is listening, **When** the harness replays a 2600 Hz burst shorter than the required hold duration (e.g. 150 ms), **Then** no detection event is reported.
6. **Given** the harness has run all clips, **When** it finishes, **Then** it prints a summary of detections, misses, false positives, and time-to-detect per clip, and exits non-zero if any expected result was not met.

---

### User Story 2 - Whistle opens a conversation with WOPR (Priority: P2)

A caller lifts the handset, the call is routed to the daemon, and they hear silence. They whistle
2600 Hz. They hear a short wink tone, then a flat synthetic voice greets them as Professor Falken.
They speak; after they pause, WOPR replies in the same voice. This repeats until they hang up.

**Why this priority**: This is the core user-facing experience and the reason the feature exists.
It depends on the detector from Story 1 being trustworthy.

**Independent Test**: Place a call (or replay a whistle clip followed by a speech clip through the
harness) and confirm the wink tone, greeting, and at least one spoken reply come back in
correctly paced audio.

**Acceptance Scenarios**:

1. **Given** a new call has been handed to the daemon, **When** no whistle has been heard, **Then** the caller hears only silence (or optional comfort noise) and nothing else.
2. **Given** a call is in the listening state, **When** a valid whistle is detected, **Then** the caller hears a short wink tone followed by WOPR's greeting addressed to Professor Falken.
3. **Given** WOPR has finished speaking, **When** the caller speaks and then stays quiet for roughly 800 ms, **Then** the caller's utterance is transcribed, sent to WOPR, and a spoken reply begins playing.
4. **Given** WOPR is replying, **When** the reply is played back, **Then** the audio arrives at the caller at real-time pace with no dropouts, stutter, or truncation.
5. **Given** any WOPR reply, **When** it is heard, **Then** it is terse, in a flat monotone synthetic voice, in the style of all-caps text, and tends to propose playing a game.
6. **Given** the caller says nothing at all after the greeting, **When** a reasonable silence elapses, **Then** the daemon keeps listening without crashing or hanging up.

---

### User Story 3 - Second whistle barges in and resets (Priority: P3)

While WOPR is mid-sentence, the caller whistles 2600 Hz again. Playback stops immediately, the
conversation history is discarded, the wink plays, and WOPR greets afresh as though the call had
just begun.

**Why this priority**: Barge-in is part of the phreaking homage (the tone always "seizes the line")
and gives the caller an escape hatch. It requires the detector to stay live during playback and
to ignore the daemon's own audio, which is the hardest correctness problem.

**Independent Test**: Through the harness, send a whistle, wait for WOPR playback to begin, send a
second whistle during playback, and confirm playback stops within a short bound and a fresh wink
plus greeting follow.

**Acceptance Scenarios**:

1. **Given** WOPR audio is playing, **When** a valid whistle is detected, **Then** outbound playback stops within 100 ms of the detection event.
2. **Given** a barge-in has occurred, **When** the next exchange happens, **Then** WOPR has no memory of the prior conversation.
3. **Given** the wink tone or WOPR speech is being emitted, **When** that audio bleeds back into the inbound stream via handset sidetone, **Then** it does not trigger a detection.
4. **Given** the daemon's own audio is playing, **When** the caller genuinely whistles over it, **Then** detection still fires (self-trigger guard must not amount to disabling barge-in).

---

### User Story 4 - Clean hangup and unattended operation on the Mac mini (Priority: P4)

The caller hangs up. The daemon tears down that call's resources promptly and continues serving
other calls. An operator, working only over SSH, can install the daemon and the language-model
runtime as boot-time background services on the headless Mac mini, wire the daemon into Asterisk
with a provided dialplan snippet, and read logs that show per-stage timing to profile latency.
After a reboot with nobody logged in, the payphone still reaches WOPR.

**Why this priority**: Required for the feature to run unattended on real hardware, but it is
infrastructure around the experience rather than the experience itself.

**Independent Test**: Through the harness, send a terminate frame mid-conversation and confirm the
connection closes and no background work lingers. Install the services on the mini, reboot it
without logging in, and confirm a harness run from the mini itself passes.

**Acceptance Scenarios**:

1. **Given** a call in any state, **When** the on-hook terminate frame arrives or the connection drops, **Then** all per-call work stops, the connection is closed, and resources are released within 1 second.
2. **Given** the daemon is running, **When** two calls are active simultaneously, **Then** each is handled independently and hanging up one does not affect the other.
3. **Given** the daemon is running at default log level, **When** a call proceeds through detection, transcription, reply generation, and playback, **Then** the log shows a detection event and the elapsed time of each stage.
4. **Given** debug logging is enabled, **When** audio is flowing, **Then** the per-window detector ratio is logged.
5. **Given** an operator follows the deployment deliverables over SSH, **When** they install the services and dialplan snippet, **Then** dialing the extension routes the call to the daemon.
6. **Given** the Mac mini is rebooted and no user logs in, **When** the boot completes, **Then** the daemon, the language-model runtime, and the phone switch are all running and a call to the extension reaches WOPR.
7. **Given** the daemon is running, **When** a call is in progress, **Then** no network traffic leaves the Mac mini other than the audio exchange with the ATA on the LAN.

---

### User Story 5 - Provision the phone switch on the Mac mini (Priority: P5)

Starting from a Mac mini with no telephony software, an operator working over SSH installs a
phone switch that supports the audio hand-off the daemon needs, registers the payphone's ATA
to it, forces the μ-law codec, and defines the extension that hands calls to the daemon. Lifting
the payphone handset produces a working call into the switch.

**Why this priority**: Without the switch nothing reaches the daemon from the real phone, but
every other story is testable through the harness without it. It is last because it is
infrastructure with the most external unknowns (macOS packaging of the switch, ATA firmware
quirks) and the least reuse of the daemon's own code.

**Independent Test**: With the daemon stopped, dial a scratch extension from the payphone that
plays a test tone or echoes audio. Then start the daemon and dial the WOPR extension.

**Acceptance Scenarios**:

1. **Given** a fresh Mac mini with SSH access, **When** the operator follows the provisioning deliverable, **Then** the phone switch is installed, starts on boot, and reports that its audio hand-off capability is loaded.
2. **Given** the switch is running, **When** the HT802 is configured with the provided settings, **Then** it registers to the switch and stays registered across an ATA reboot.
3. **Given** the ATA is registered, **When** the payphone handset is lifted and the WOPR extension is dialed (or auto-dialed), **Then** the call is answered and handed to the daemon within 2 seconds, and the daemon logs a call start.
4. **Given** a call is established, **When** the negotiated codec is inspected, **Then** it is μ-law end to end, and the ATA has echo cancellation, voice activity detection, and silence suppression disabled.
5. **Given** the payphone is on-hook, **When** the switch's registration state is inspected after 24 hours, **Then** the ATA is still registered.

---

### Edge Cases

- **Whistle with brief dropouts**: a human whistle may falter for a window or two mid-run. A small number of missed windows inside the hold period must not reset the run.
- **Whistle that starts strong then fades**: detection is ratio-based so level decline alone should not break the run; a drop below usable signal should reset it.
- **Caller starts talking while WOPR is still speaking** (without whistling): current behaviour is to keep playing; speech does not barge in. Only the whistle does.
- **Transcription returns empty text** (silence, breath, cough): daemon should not send an empty turn to WOPR; it returns to listening.
- **Reply generation fails or times out** (language-model runtime not running, model not pulled, request too slow): the caller hears a short canned WOPR-style error line and the daemon returns to listening rather than dropping the call.
- **Cold language model after idle**: the first exchange after a long idle period may need the model loaded into memory. It must still complete within a bounded time, and subsequent exchanges must meet the normal latency budget. The runtime should be configured to keep the model resident.
- **Memory pressure on a small Mac mini**: speech recognition, synthesis, and the language model share RAM. The chosen model size must leave headroom; the provisioning deliverable states the minimum RAM and the model to use for it.
- **Speech synthesis fails**: the daemon logs the error and returns to listening.
- **Hangup while transcription or reply generation is in flight**: in-flight work is cancelled or its result discarded; nothing is written to a closed connection.
- **Malformed or unexpected frame type**: logged and either ignored or treated as terminate, depending on severity; the daemon process itself never crashes.
- **Audio frame of unexpected length**: tolerated by buffering to fixed window boundaries rather than assuming every frame is exactly 20 ms.
- **Very long caller monologue**: endpointing must still fire; an upper cap on utterance length prevents unbounded buffering.
- **Barge-in whistle immediately after wink**: handled identically to any other barge-in; results in another wink and fresh greeting.

## Requirements *(mandatory)*

### Functional Requirements

**Call handling**

- **FR-001**: The daemon MUST accept incoming AudioSocket connections and handle each call concurrently and independently.
- **FR-002**: On a new call the daemon MUST send only silence (or optional comfort noise) to the caller until a whistle is detected.
- **FR-003**: The daemon MUST tear down all per-call state and release resources when the terminate frame arrives or the connection drops, without affecting other calls.
- **FR-004**: The daemon MUST never write to a connection after it has been closed or terminated.

**Whistle detection**

- **FR-005**: The daemon MUST detect a sustained tone at 2600 Hz in the inbound audio using a relative (ratio-based) energy test against neighbouring frequencies, so that detection is insensitive to absolute signal level.
- **FR-006**: Detection MUST require the tone to be present for a configurable hold duration (default 300 ms) before firing, and MUST tolerate a configurable small number of dropout windows (default 2) within that hold.
- **FR-007**: Detection MUST tolerate pitch drift of at least ±30 Hz around 2600 Hz, amplitude wobble, and breath noise consistent with a human whistle through a payphone handset.
- **FR-008**: Detection MUST NOT fire on ordinary speech, nor on speech containing sustained energy at 2500 Hz or 2700 Hz.
- **FR-009**: Detection MUST NOT fire on bursts shorter than the hold duration.
- **FR-010**: The detector MUST remain active during all call states, including while the daemon is playing audio to the caller.
- **FR-011**: The daemon's own outbound audio (wink tone, synthesized speech) bleeding back through handset sidetone MUST NOT trigger detection. This MUST be achieved without disabling detection during playback (e.g. by a brief post-emission suppression window or by tracking recently played audio).
- **FR-012**: All detector tuning values (window size, frequency bins, ratio threshold, hold count, miss budget) MUST be declared as named constants in one place near the top of the daemon source so they can be tuned.

**Conversation**

- **FR-013**: On detection the daemon MUST play a short wink tone (brief 480 Hz burst) and then enter conversation mode.
- **FR-014**: On entering conversation mode the daemon MUST play a WOPR greeting addressed to Professor Falken.
- **FR-015**: In conversation mode the daemon MUST capture caller speech and end the utterance after approximately 800 ms of silence, using an energy-based voice activity test.
- **FR-016**: Each completed utterance MUST be transcribed locally, sent to a language model running on the same machine with the WOPR persona and conversation history, and the reply synthesized to speech and played back. No step may call a remote service.
- **FR-017**: Empty or whitespace-only transcriptions MUST be discarded without a model round-trip.
- **FR-018**: The WOPR persona MUST be terse, flat in affect, styled as all-caps text, address the caller as "Professor Falken," and consistently steer toward proposing games, including global thermonuclear war.
- **FR-019**: On a second (or subsequent) whistle detection at any point in conversation mode, the daemon MUST stop outbound playback immediately, discard conversation history, and restart from the wink and greeting.
- **FR-020**: If transcription, reply generation, or speech synthesis fails, the daemon MUST log the failure, optionally play a short canned WOPR-style line, and return to listening; it MUST NOT drop the call.

**Audio transport**

- **FR-021**: All outbound audio MUST be delivered in the same fixed-size framing the daemon receives, paced at real time (one frame every 20 ms), never dumped as a burst.
- **FR-022**: All synthesized or generated audio MUST be converted to the call's native format (8 kHz, mono, 16-bit signed linear) before transmission.

**Observability**

- **FR-023**: The daemon MUST emit structured logs with a per-call identifier on every line.
- **FR-024**: At DEBUG level the daemon MUST log the detector ratio for every analysis window.
- **FR-025**: At INFO level the daemon MUST log every detection event, barge-in, state transition, and call start/end.
- **FR-026**: The daemon MUST log elapsed time for each pipeline stage (utterance capture, transcription, reply generation, synthesis, playback start) so end-to-end latency can be profiled.

**Test harness**

- **FR-027**: A test harness MUST synthesize, without any phone hardware, at minimum: a clean steady 2600 Hz tone; a whistle-like tone with ±30 Hz drift, amplitude wobble, and breath noise; a speech-only clip; a speech clip with sustained 2600-adjacent (2500/2700 Hz) harmonic content; and a sub-hold-duration 2600 Hz burst.
- **FR-028**: The harness MUST replay these clips into the daemon over the same protocol and framing Asterisk uses, at real-time pace, acting as the Asterisk side of the connection.
- **FR-029**: The harness MUST determine, for each clip, whether detection fired and how long after tone onset, and MUST report detection rate, false-positive rate, and time-to-detect.
- **FR-030**: The harness MUST be able to exercise barge-in (whistle during playback) and hangup (terminate frame mid-call) scenarios.
- **FR-031**: The harness MUST exit non-zero when any clip's outcome differs from its expected outcome, so it can serve as a regression test.

**Deliverables**

- **FR-032**: The daemon MUST be delivered as a single source file.
- **FR-033**: The deliverables MUST include a dependency manifest, boot-time background service definitions for the daemon and the language-model runtime that work on a headless macOS host with no logged-in user, and an Asterisk dialplan snippet for the extension.
- **FR-034**: During a call the daemon MUST make no network connections other than to the phone switch and the language-model runtime on the local host. Model downloads happen only at provisioning time.

**Provisioning (Mac mini)**

- **FR-035**: The deliverables MUST include a provisioning runbook or script, executable over SSH, that takes a Mac mini from no telephony software to a running phone switch with the audio hand-off capability the daemon uses.
- **FR-036**: The provisioning deliverable MUST include the switch-side configuration that registers the HT802 as an endpoint, restricts the codec to μ-law, and defines the WOPR extension.
- **FR-037**: The provisioning deliverable MUST document the ATA-side settings required (registration target, codec, echo cancellation off, voice activity detection off, silence suppression off, and optional off-hook auto-dial of the WOPR extension).
- **FR-038**: The phone switch, the language-model runtime, and the daemon MUST all start automatically on boot without a logged-in desktop session.
- **FR-039**: The provisioning deliverable MUST pull the required speech-recognition and language models so that no download is needed at call time.
- **FR-040**: The provisioning deliverable MUST be re-runnable without damaging an existing working install.

### Key Entities

- **Call Session**: One active phone call. Holds the call identifier received in the first frame, the current state (listening / conversing / playing), the detector's running window state, the suppression state for self-trigger guarding, the buffered caller utterance, and the conversation history. Created on connect, destroyed on terminate or disconnect.
- **Detector Window**: A 10 ms slice of inbound audio and its computed energy ratio; a run of consecutive passing windows (with a bounded miss budget) constitutes a detection.
- **Utterance**: A contiguous span of caller audio bounded by the silence endpoint, submitted for transcription.
- **Conversation Turn**: One caller utterance (as text) and the WOPR reply (as text and as synthesized audio). The ordered list of turns is the conversation history, cleared on barge-in.
- **Test Clip**: A synthesized audio recording with a label, an expected outcome (detect / no detect), and, for detect clips, the tone onset time used to compute time-to-detect.

## Constraints *(user-mandated)*

These are explicit directives from the feature request. They are recorded here because they bound
the design, not because the spec chooses them. Planning MUST honour them.

- Transport is AudioSocket over TCP with frames of one type byte, a two-byte big-endian length, and payload. Types: terminate, UUID (first frame, 16 bytes), audio, error.
- Audio is 8 kHz mono signed 16-bit little-endian, 20 ms frames of 160 samples (320 bytes), in both directions. Asterisk handles the μ-law leg; the daemon never sees μ-law.
- Detection uses the Goertzel algorithm on 10 ms windows (80 samples), three bins at 2500 / 2600 / 2700 Hz, ratio `energy(2600) / (energy(2500) + energy(2700))` against a threshold near 0.35, with a hold of 30 consecutive passing windows and a small miss budget.
- Speech-to-text: `faster-whisper`, model `base.en`, `int8`, running locally.
- Language model: a local model served by **Ollama** on the Mac mini (clarified 2026-09-04; supersedes the original Anthropic API / `claude-sonnet-4-6` directive). The specific model is a tunable chosen during planning to fit the mini's RAM.
- Text-to-speech: `espeak-ng` with the `+croak` voice, resampled to 8 kHz. This is a deliberate period choice (Votrax SC-01 homage). A neural voice MUST NOT be substituted.
- Implementation: Python, `asyncio`, single file, one task per connection.
- Host: one Apple Silicon Mac mini running macOS, headless, administered over SSH. Asterisk, Ollama, the daemon, and all models live on it. Nothing required at call time may live anywhere else.
- Asterisk is not installed on the mini today. Installing it, with AudioSocket support, is in scope.
- Dependencies beyond those listed above require asking the user first.
- The test harness is built before the daemon's conversation features.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A clean 2600 Hz tone is detected in 100% of harness runs, with time-to-detect under 500 ms from tone onset.
- **SC-002**: A drifting, wobbling, breathy synthetic whistle (±30 Hz) is detected in at least 95% of harness runs.
- **SC-003**: Speech-only and 2600-adjacent-harmonic clips produce zero detections across the full harness suite (0% false-positive rate on the provided corpus).
- **SC-004**: Sub-hold-duration bursts produce zero detections.
- **SC-005**: On a real payphone, a person who can whistle a reasonably steady 2600 Hz reaches WOPR on the first or second attempt in at least 8 of 10 trials.
- **SC-006**: The daemon's own wink tone and speech never trigger a detection across a 10-minute continuous conversation test with sidetone present.
- **SC-007**: After the caller stops speaking, WOPR's reply begins playing within 5 seconds in the typical case on the Mac mini with the model already resident, within 20 seconds for the first exchange after a long idle, and every stage's contribution to that latency is visible in the logs.
- **SC-008**: Barge-in halts playback within 100 ms of the detection event in 100% of harness runs.
- **SC-009**: Hanging up releases all per-call resources within 1 second, and 100 consecutive connect/hangup cycles leave no lingering tasks, open sockets, or memory growth beyond noise.
- **SC-010**: An operator with SSH access and the deliverables can go from a Mac mini with no telephony software to a payphone that reaches WOPR in under 2 hours of wall-clock time, excluding model download time, without additional undocumented steps.
- **SC-012**: After a reboot with no user logged in, the payphone reaches WOPR within 3 minutes of power-on with no operator action.
- **SC-013**: With the mini's uplink disconnected from the internet (LAN only), a full whistle-to-reply exchange still succeeds.
- **SC-014**: The ATA remains registered to the switch for 7 consecutive days without manual intervention.
- **SC-011**: Playback audio at the caller's ear has no audible dropouts or stutter over a 60-second reply.

## Assumptions

- **Greeting on connect**: After the wink, WOPR speaks first with a greeting to Professor Falken rather than waiting for the caller. This matches the film and gives the caller an immediate cue that the whistle worked.
- **Idle pre-detection audio**: Pure digital silence is the default; comfort noise is an optional tweak, not a requirement.
- **Speech does not barge in**: Only the whistle interrupts playback. Caller speech during WOPR's reply is ignored until playback ends.
- **Self-trigger guard approach**: A short suppression window after the daemon emits audio is the default approach; correlating against recently played audio is an acceptable alternative if suppression proves too coarse. Either way, a genuine whistle over playback must still be detectable.
- **Silence endpoint**: ~800 ms of low energy ends an utterance. A minimum utterance length (to skip clicks) and a maximum utterance length (to bound buffering) are reasonable additions and will be tunable constants.
- **Error line on failure**: A short canned WOPR line (e.g. "A STRANGE GAME. PLEASE REPEAT, PROFESSOR FALKEN.") is played when the model or transcriber fails. Wording is not fixed.
- **Concurrency**: One physical payphone means one call at a time in practice, but the daemon handles multiple connections since the design is one task per connection. The transcription model is loaded once and shared.
- **Speech clips for the harness**: Generated with the same speech synthesizer used for WOPR, so the harness adds no dependencies. Harmonic-rich variants are made by mixing in sustained tones near 2600 Hz.
- **Implied dependencies**: The speech-to-text library, the espeak-ng system binary, the Ollama runtime, numpy (approved 2026-09-04), and whatever the phone switch needs to build or install on macOS are treated as approved by the feature request and clarifications. Anything else is a planning-phase question to raise with the user.
- **Deployment target**: One Apple Silicon Mac mini running a current macOS, headless, reachable over SSH with an administrator account that can use sudo and install Homebrew packages. Background services are managed by the macOS service manager as system-level daemons so they run with no user logged in.
- **Hardware sizing**: The mini is assumed to have at least 16 GB of unified memory, enough for an 8B-class quantized language model alongside speech recognition. If it has 8 GB, a smaller model is substituted; the provisioning deliverable names both options.
- **Language model choice**: An instruction-tuned open model in the 7-8B class is assumed adequate for WOPR's terse persona. The exact model is a tunable, not a spec commitment.
- **Asterisk on macOS**: Assumed installable via Homebrew or a source build with the AudioSocket application included. If a source build is required, the provisioning deliverable scripts it. Asterisk 18 or later is assumed.
- **ATA**: The Grandstream HT802 is already on the LAN and its web interface is reachable from the mini. Its configuration is applied manually through that interface following the documented settings; automating ATA provisioning is out of scope.
- **Network**: The mini and the ATA are on the same LAN segment with no NAT between them. Internet access is available during provisioning for package and model downloads only.
- **Out of scope**: Call recording, multi-language support, a web UI, telemetry beyond logs, automated ATA provisioning, remote administration beyond SSH, and any coin-collection or billing behaviour of the payphone.
