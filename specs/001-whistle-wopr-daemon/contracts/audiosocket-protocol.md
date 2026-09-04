# Contract: AudioSocket wire protocol (daemon as server)

The daemon implements the Asterisk AudioSocket protocol as a TCP **server**. Asterisk (or the
harness) is the **client**. This file states what the daemon guarantees and requires.

## Framing

```
+--------+----------------+------------------+
| type   | length (BE u16)| payload          |
| 1 byte | 2 bytes        | `length` bytes   |
+--------+----------------+------------------+
```

| Type | Name | Payload | Direction | Daemon behaviour |
|---|---|---|---|---|
| `0x00` | TERMINATE | 0 bytes | client → daemon | Ends the call. Daemon closes the socket. |
| `0x01` | UUID | 16 bytes | client → daemon | MUST be the first frame. Becomes the call id. Any other first frame, or length ≠ 16, is logged at ERROR and the socket is closed. |
| `0x10` | AUDIO | signed 16-bit LE PCM, 8 kHz mono | both | Inbound: appended to the accumulator, processed per 160-byte window; any length ≥ 2 accepted. Outbound: always exactly 320 bytes (20 ms). |
| `0xff` | ERROR | 1 byte code | client → daemon | Logged at WARNING. Three consecutive ERROR frames are treated as TERMINATE. |
| other | — | any | client → daemon | Logged at WARNING and ignored. |

## Timing guarantees (daemon → client)

- After the UUID frame is accepted, the daemon sends one AUDIO frame every 20 ms ± 2 ms
  (monotonic clock), continuously, until the call ends. When it has nothing to say the frame is
  digital silence (320 zero bytes).
- The daemon never sends more than one frame ahead of real time. A burst of N frames is a bug.
- On barge-in, the outbound queue is dropped; at most one already-scheduled frame follows the
  detection event, so audible output stops within ≈ 40 ms at the socket.

## Lifecycle

1. Client connects, sends UUID frame.
2. Daemon starts sending silence frames immediately.
3. Client streams AUDIO frames at 20 ms pace.
4. On whistle: daemon's outbound frames carry a 480 Hz wink burst (default 200 ms) followed by
   synthesized speech.
5. Client sends TERMINATE, or closes the socket. Daemon closes its side within 1 s and frees all
   per-call state. The daemon does not send TERMINATE itself.

## What the client may rely on for testing

- The **first non-silent outbound audio after connection** is the wink tone at 480 Hz. A client
  can detect the daemon's "whistle heard" event purely from return audio.
- After barge-in, the next non-silent outbound audio is again the wink tone.
- Outbound frame cadence is a valid measure of pacing correctness.
