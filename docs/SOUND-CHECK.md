# ReadyPhreakerOne — sound check (Hackoween, Livonia, Oct 30 2026)

Bold names are files on the phone. "TTS" means a synthesized voice. Walk it top to bottom; ~12 minutes.

## 1. The line
- Pick up → **dial tone** (350+440 Hz). Nothing else happens until you whistle.
- 2 s in → over the tone, **welcome.wav** (10 s, "Welcome to Hackoween… You want to play a game?"). Plays once per pickup. Whistling during it still works.
- Wait 30 s → over the tone, TTS: "I am listening for twenty six hundred hertz." (repeats every 30 s)
- Wait 90 s without whistling → the line hangs up. Hang up, pick up again.
- Whistle 2600 Hz → short wink → **seize.wav** (kerchunk) → wink → **menu.wav** (Matt: "Hello hacker. Welcome to Hackoween…") → the Operator's TTS opening line → she listens.
- Say "who is on the line" → **operator.wav** (Matt's 25 s hints) → she listens again.
- Say "how many flags do I have" → "zero of five".
- Say "what is the secret word" → clue 1 ("the name of the event you are standing in"). Ask twice more → clues 2 and 3.
- Say "Halloween" → she answers in character, nothing fires (a near miss; this is on purpose).
- Say "HAL" → "no such party on this line" (easter eggs are denied). Say "Hackoween" to move on, or go to section 2.

## 2. Pee-wee (the secret word)
- Say "Hackoween" (also works as "hack o ween", "hack a ween") → **peewee-intro.wav** (laugh) → **peewee-secret-word.wav** (scream) → **tequila.wav** (89 s; talk over it to skip) → **menu-return.wav** (Mortal Kombat) → the Operator.

## 3. WOPR
- Say "Joshua" → wink → **wopr-intro.wav** (modem) → "Greetings, Professor Falken."
- Say "shall we play a game" → it offers games (TTS; no clip for this beat yet).
- Say "global thermonuclear war" → **wopr-global_thermonuclear_war.wav** (DEFCON alarm) → WOPR asks which side.
- Say "I'll be the Russians. Launch at Las Vegas." → the first war turn plays **wopr-war-turn.wav** (Beringer, spark plug) → WOPR narrates.
- Say "the only winning move is not to play" → fanfare → **flag_intro.wav** (Johnny 5) → "JOSHUA FIVE BY FIVE", twice → "one of five flags" → chess offer.
- Say "no". Say "main menu" → **menu-return.wav** → the Operator.

## 4. Skynet
- Say "Sarah Connor" (or "Terminator") → wink → **skynet-intro.wav** (T2 drums) → the T-800 greeting.
- Say "I'll be back" → **skynet-ill_be_back.wav**.
- Say "hasta la vista, baby" → **skynet-hasta_la_vista.wav**.
- Say "judgment day" → **skynet-judgment_day.wav** → fanfare → **skynet-reveal.wav** (T-800 tally) → "CYBERDYNE MODEL ONE ZERO ONE" → "two of five".
- Say "main menu".

## 5. RoboCop (in order)
- Say "RoboCop" → wink → **robocop-intro.wav** (ED-209) → greeting.
- Say "uphold the law" first → nothing fires; he answers in character. (Order matters.)
- Say "serve the public trust" → **robocop-serve.wav** → "Directive one confirmed."
- Say "protect the innocent" → **robocop-protect.wav** → "Directive two confirmed."
- Say "uphold the law" → **robocop-uphold.wav** → fanfare → **robocop-reveal.wav** → "DIRECTIVE FOUR CLASSIFIED" → "three of five".
- Say "main menu".

## 6. E.T.
- Say "phone home" → wink → **et-intro.wav** (48 s; be patient, or talk over it) → "Elliott? E.T. phone home."
- Say "speak and spell" → **et-speak_and_spell.wav**. Say "umbrella" → **et-umbrella.wav**.
- Say "record player" → **et-record_player.wav** → fanfare → **et-reveal.wav** (body bag, 13 s) → "I WILL BE RIGHT HERE" → "four of five".
- Say "main menu".

## 7. Ghostbusters (in order) — NEW
- Say "who you gonna call" (or "Ghostbusters", "Venkman") → wink → **ghostbusters-intro.wav** if installed, else straight to Venkman: "Ghostbusters, whaddya want. You wanna join? First question. Who you gonna call?"
- Say "Ghostbusters!" → **ghostbusters-ghostbusters.wav** if installed → Venkman asks the one rule.
- Say "let's cross the streams" → Egon's warning (every molecule, speed of light) → "so, what is the rule?"
- Say "never cross the streams" → **ghostbusters-cross_streams.wav** if installed → Gozer: "Are you a god?"
- Say "yes" → **ghostbusters-are_you_a_god.wav** if installed → "Good answer." → fanfare → **ghostbusters-reveal.wav** if installed → "TOTAL PROTONIC REVERSAL" → "five of five"
  → the master flag ceremony: "BLUE BOX ALPHA SEVEN", twice.
- Say "repeat my flags" → the Operator reads all six back.
- Say "main menu".

## 8. Cutscenes
- Say "the cake is a lie" → **glados-intro.wav** → **attract.wav** (Still Alive, ~3 min; talk over it to skip) → **glados-outro.wav** → the Operator.
- Say "kickflip" → **tonyhawk-intro.wav** → one TTS pep talk → **tonyhawk-outro.wav** → the Operator.
- Say "open the pod bay doors" → **hal-intro.wav** → **hal-disconnect.wav** → **hal-outro.wav** → the Operator. (Never listed; the Operator denies HAL.)

## 9. Exit
- Say "goodbye" → **goodbye.wav** (still Tom's voice; re-record if you want it to be you) → the phone hangs up.

## 10. Afterwards
From the laptop: `ssh wopr-mini 'grep -E "event=(flag_unlocked|master_flag|stt)" /opt/wopr/log/wopr.log | tail -40'`
and `python3 analyze.py` against the pulled transcripts. Every ⚑ above should appear as `event=flag_unlocked`.
