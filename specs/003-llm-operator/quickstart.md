# Quickstart: Validate the Conversational Operator

Assumes the daemon runs on the mini from `/opt/wopr` and Ollama + Whisper are warm.

## 1. Unit tests — no phone needed

```
./venv/bin/python -m unittest tests.test_dsp
```

Expected: green, including `OperatorTests`: directory renders only listed games (HAL absent),
`operator_filter` strips unknown names and the secret word, clue requests progress in order and
exhaust, the nudge fires on a capture timeout, and the fallback selector picks the menu loop when
the LLM is unavailable.

## 2. Reach the Operator

Pick up (silence), whistle. Expected: wink → kerchunk → the recorded greeting → silence (she is
listening; no menu loop). Log: `event=experience key=operator`, no attract frames.

## 3. The directory, and only the directory

Ask: "what can I do here?", "who is on this line?", "how do I reach the war computer?".
Expected: short in-character answers naming only listed games; each `event=llm` is followed by an
`event=operator_filter` line showing nothing removed. Ask "what's the weather?" → a brush-off.

## 4. Secret word

Ask "what's the secret word?" three times. Expected: three different clues in order
(`event=clue n=1..3`), never the word; a fourth ask gives the exhausted line. Then growl "GrrCon" →
Pee-wee plays.

## 5. Easter egg stays hidden

Ask "is HAL on this line?" Expected: an in-character denial; the log's filter line shows any
"HAL" token removed if the model tried. Then say "open the pod bay doors" → HAL plays.

## 6. Connect, return, nudge, exit

Say "RoboCop" to her → RoboCop. Say "main menu" → she acknowledges. Stay silent → one nudge at
~12 s (`event=nudge`), another ~12 s later. Say "list the options" → the recorded hints clip.
Say "goodbye" → sign-off → hang-up.

## 7. Degraded mode

`ssh wopr-mini 'sudo launchctl kickstart -k system/com.crunchwhistle.ollama'` and call within a
few seconds, or restart the daemon with `WOPR_OPERATOR=0`. Expected: after the whistle, the
recorded menu loop plays (`event=state to=ARMED`, attract frames) and "RoboCop" still connects.

## 8. Latency

From the log, `stt ms` + `llm ms` + `tts ms` for an Operator turn ≤ ~3000 ms; a forced slow
reply (e.g. `WOPR_OPERATOR_BUDGET=0.5`) yields `event=operator_fallback reason=timeout` and the
fixed line, with the call still live.
