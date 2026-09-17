# CrunchWhistle — Greeter Voice Recording Script

Thanks for lending your voice! This is the **greeter** for a payphone art installation at
GRRCon — a mysterious phone hackers pick up. You're the friendly, slightly-conspiratorial
operator who welcomes them and hints at what they can do. Think **1980s phone operator meets
game-show host**, warm and a little mysterious.

Two short clips. That's it.

---

## How to record (technical — quick)

- **Format:** WAV (PCM). Any sample rate is fine (44.1 kHz or 48 kHz is great) — the system
  downsamples to telephone quality automatically.
- **Mono** preferred (stereo is fine too).
- **Quiet room**, consistent distance from the mic, no background music.
- **Trim** the silence off the very start and end of each file. Short pauses *inside* a line are
  good (they help pacing) — see the `/` marks.
- Because it ends up as 8 kHz phone audio, **enunciate a little more than normal** and don't
  drop to a whisper. Punchy and clear beats subtle.
- One clean take each is all we need. Send them as **`menu.wav`** and **`operator.wav`**.

---

## Clip 1 — `menu.wav`  (the pickup greeting)

This plays the instant someone lifts the handset, then repeats every ~10 seconds until they
respond. Keep it short, warm, inviting. **Do not add trailing silence** — the system adds the
pause automatically.

> **"Hello, hacker. / Welcome to GRRCon. / How may I help you?"**

*Direction:* welcoming, a touch playful — like you've been expecting them.

---

## Clip 2 — `operator.wav`  (the hints)

This plays when the caller says **"operator"** (or "help" / "menu"). It gives them the
passphrases without spoiling what each one summons. Let the mystery hang.

> **"Here's what you can do. / To reach a certain war computer — whistle twenty-six hundred
> hertz, / or say: Joshua. / Say: Sarah Connor... / or John Connor. / Say: RoboCop. / Or say...
> the cake is a lie. / Go on. / Try one."**

*Direction:* conspiratorial, like you're slipping them secret codes. Slow down and land each
passphrase clearly — people will be repeating them back into the phone, so **Joshua**, **Sarah
Connor**, **John Connor**, **RoboCop**, and **the cake is a lie** must be crisp.

---

## Notes

- The passphrases must be said **exactly** as written so players can echo them: *Joshua*,
  *Sarah Connor*, *John Connor*, *RoboCop*, *the cake is a lie*, and the whistle is **2600 Hz**.
- The characters themselves (the war computer, the terminator, the cop, the singing AI) have
  their own robotic/AI voices generated live — you're **only** the greeter.
- Want to add more later (a "thanks for playing" sign-off, alternate greetings, holiday
  variants)? Easy to drop in — just record more clips and we'll wire them up.

Drop the two WAVs and we'll hear your voice on the phone.
