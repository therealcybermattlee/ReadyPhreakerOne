"""Unit tests for the pure-DSP, framing, detector, VAD, persona, and parsing helpers in wopr_daemon.py.

Run: venv/bin/python -m unittest discover -s tests -v
No network, no espeak-ng, no Whisper. numpy only.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wopr_daemon as w  # noqa: E402


def tone(hz, ms=10, amp=0.25, phase=0.0):
    return w.synth_tone(hz, ms, amp, phase=phase)


class FramingTests(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        payload = bytes(range(256)) * 2
        frame = w.encode_frame(w.FrameType.AUDIO, payload)
        self.assertEqual(frame[0], 0x10)
        self.assertEqual(int.from_bytes(frame[1:3], "big"), len(payload))
        ftype, got, rest = w.decode_frame(frame + b"tail")
        self.assertEqual((ftype, got, rest), (0x10, payload, b"tail"))

    def test_odd_lengths_and_empty(self):
        for n in (0, 1, 3, 159, 161, 320, 641):
            f = w.encode_frame(w.FrameType.AUDIO, b"\x01" * n)
            self.assertEqual(w.decode_frame(f)[1], b"\x01" * n)
        self.assertEqual(w.encode_frame(w.FrameType.TERMINATE), b"\x00\x00\x00")
        with self.assertRaises(ValueError):
            w.decode_frame(b"\x10\x00")

    def test_frames_from_pcm_pads_last_frame(self):
        pcm = np.arange(500, dtype="<i2")  # 1000 bytes → 3 full + 1 partial frame
        frames = w.frames_from_pcm(pcm)
        self.assertEqual(len(frames), 4)
        self.assertTrue(all(len(f) == w.FRAME_BYTES for f in frames))
        self.assertEqual(frames[-1][-2:], b"\x00\x00")
        self.assertEqual(b"".join(frames)[:1000], pcm.tobytes())

    def test_pcm_windows(self):
        pcm = np.arange(250, dtype="<i2").tobytes()
        wins = w.pcm_windows(pcm)
        self.assertEqual(len(wins), 3)
        self.assertEqual(wins[2][0], 160)

    def test_silence_frame_shape(self):
        self.assertEqual(len(w.SILENCE_FRAME), 3 + w.FRAME_BYTES)


class GoertzelTests(unittest.TestCase):
    def test_vectorized_matches_reference(self):
        x = tone(2600)
        ref = w.goertzel_energy(x, 26)
        vec = float(w.window_energies(x)[1])
        self.assertAlmostEqual(ref / vec, 1.0, places=6)

    def test_on_bin_tone_has_high_ratio_and_half_purity(self):
        ratio, e26, purity = w.window_ratio(tone(2600))
        self.assertGreater(ratio, 10)
        self.assertGreater(e26, w.MIN_BIN_ENERGY)
        self.assertAlmostEqual(purity, 0.5, places=2)

    def test_neighbour_tones_fail_ratio(self):
        for hz in (2500, 2700):
            ratio, _, _ = w.window_ratio(tone(hz))
            self.assertLess(ratio, w.RATIO_THRESHOLD)

    def test_drift_within_spec_passes(self):
        # ±30 Hz required; verify a bit beyond at various phases
        for hz in (2570, 2585, 2600, 2615, 2630, 2640):
            for ph in (0.0, 1.0, 2.5):
                ok, ratio, e26, purity = w.window_passes(tone(hz, phase=ph))
                self.assertTrue(ok, f"{hz} Hz phase {ph}: ratio={ratio:.2f} purity={purity:.2f}")

    def test_far_tone_leakage_rejected_by_purity(self):
        # A loud far-off pure tone leaks into all three bins ~equally (ratio ≈ 0.5) and can
        # clear the absolute floor; the purity gate must reject it.
        for hz in (480, 1000, 1800, 3300):
            ok, ratio, e26, purity = w.window_passes(tone(hz, amp=0.9))
            self.assertFalse(ok, f"{hz} Hz passed: ratio={ratio:.2f} e26={e26:.3g} purity={purity:.3f}")
            self.assertLess(purity, 0.01)

    def test_silence_and_hiss_rejected(self):
        self.assertFalse(w.window_passes(np.zeros(80, dtype="<i2"))[0])
        rng = np.random.default_rng(1)
        hiss = (rng.standard_normal(80) * 30).astype("<i2")
        ok, _, e26, _ = w.window_passes(hiss)
        self.assertFalse(ok)
        self.assertLess(e26, w.MIN_BIN_ENERGY)

    def test_rms(self):
        self.assertAlmostEqual(w.rms(np.zeros(80, dtype="<i2")), 0.0)
        self.assertAlmostEqual(w.rms(tone(1000, amp=0.5)) / (0.5 * 32767 / np.sqrt(2)), 1.0, places=1)


class DetectorTests(unittest.TestCase):
    def feed(self, det, wins, masked=False):
        return [det.update(x, masked) for x in wins]

    def test_fires_after_hold(self):
        det = w.ToneDetector()
        wins = [tone(2600, phase=i) for i in range(w.HOLD_WINDOWS)]
        fired = self.feed(det, wins)
        self.assertEqual(sum(fired), 1)
        self.assertTrue(fired[-1])

    def test_miss_budget_tolerated_but_not_exceeded(self):
        silence = np.zeros(80, dtype="<i2")
        # two dropouts inside the run → still fires within HOLD + 2 windows
        det = w.ToneDetector()
        seq = [tone(2600)] * 10 + [silence] + [tone(2600)] * 10 + [silence] + [tone(2600)] * 12
        self.assertEqual(sum(self.feed(det, seq)), 1)
        # three dropouts → run resets, no fire in the same span
        det = w.ToneDetector()
        seq = [tone(2600)] * 8 + [silence] + [tone(2600)] * 8 + [silence] + [tone(2600)] * 8 + [silence] + [tone(2600)] * 8
        self.assertEqual(sum(self.feed(det, seq)), 0)

    def test_short_burst_does_not_fire(self):
        det = w.ToneDetector()
        self.assertEqual(sum(self.feed(det, [tone(2600)] * 15)), 0)

    def test_long_whistle_fires_once_until_rearmed(self):
        det = w.ToneDetector()
        fired = self.feed(det, [tone(2600)] * 200)     # 2 s continuous
        self.assertEqual(sum(fired), 1)
        # quiet for REARM_WINDOWS, then a new whistle fires again
        self.feed(det, [np.zeros(80, dtype="<i2")] * w.REARM_WINDOWS)
        self.assertEqual(sum(self.feed(det, [tone(2600)] * w.HOLD_WINDOWS)), 1)

    def test_masked_windows_freeze_the_run(self):
        det = w.ToneDetector()
        self.feed(det, [tone(2600)] * 20)
        self.feed(det, [tone(2600)] * 50, masked=True)   # would fire if counted
        self.assertEqual(det.run, 20)
        self.assertTrue(self.feed(det, [tone(2600)] * 10)[-1])


class VadTests(unittest.TestCase):
    def test_endpoint_after_silence(self):
        rng = np.random.default_rng(2)
        vad = w.VadState()
        quiet = lambda: (rng.standard_normal(80) * 20).astype("<i2")  # noqa: E731
        loud = lambda: (rng.standard_normal(80) * 3000).astype("<i2")  # noqa: E731
        out = None
        for _ in range(30):                       # 300 ms floor init + idle
            out = vad.update(quiet())
            self.assertIsNone(out)
        for _ in range(60):                       # 600 ms speech
            out = vad.update(loud())
            self.assertIsNone(out)
        n_end = None
        for i in range(120):                      # up to 1.2 s silence
            out = vad.update(quiet())
            if out is not None:
                n_end = i + 1
                break
        self.assertIsNotNone(out, "utterance never endpointed")
        self.assertAlmostEqual(n_end * 10, w.ENDPOINT_MS, delta=20)
        # length: preroll + 600 ms speech + ~200 ms kept silence
        self.assertGreater(len(out) / 16, 600)
        self.assertLess(len(out) / 16, 600 + w.VAD_PREROLL_MS + 300)

    def test_short_click_discarded(self):
        rng = np.random.default_rng(3)
        vad = w.VadState()
        for _ in range(30):
            vad.update((rng.standard_normal(80) * 20).astype("<i2"))
        for _ in range(10):                       # 100 ms burst < MIN_UTTERANCE_MS
            vad.update((rng.standard_normal(80) * 3000).astype("<i2"))
        outs = [vad.update((rng.standard_normal(80) * 20).astype("<i2")) for _ in range(120)]
        self.assertTrue(all(o is None for o in outs))


class PersonaTests(unittest.TestCase):
    def test_postprocess_strips_think_and_markdown_and_uppercases(self):
        raw = "<think>I should propose a game.</think>\n**Greetings** _Professor_ Falken. Shall we `play`?"
        self.assertEqual(w.postprocess_reply(raw), "GREETINGS PROFESSOR FALKEN. SHALL WE PLAY?")

    def test_postprocess_truncates_at_sentence(self):
        long = ("A fine game of chess awaits you professor falken. " * 12)
        out = w.postprocess_reply(long)
        self.assertLessEqual(len(out), w.MAX_REPLY_CHARS)
        self.assertTrue(out.endswith("."))

    def test_postprocess_empty(self):
        self.assertEqual(w.postprocess_reply(""), "")
        self.assertEqual(w.postprocess_reply("<think>only thoughts</think>"), "")


class OllamaParsingTests(unittest.TestCase):
    def test_request_body_shape(self):
        body = w.ollama_request_body("qwen3:8b", [{"role": "user", "content": "hi"}])
        self.assertEqual(body["model"], "qwen3:8b")
        self.assertFalse(body["stream"])
        self.assertEqual(body["keep_alive"], -1)
        self.assertIs(body["think"], False)
        self.assertEqual(body["options"]["num_predict"], w.LLM_NUM_PREDICT)
        self.assertEqual(body["options"]["num_ctx"], w.LLM_NUM_CTX)

    def test_response_parsing_ns_to_ms(self):
        raw = json.dumps({"message": {"role": "assistant", "content": "SHALL WE PLAY A GAME?"},
                          "done": True, "done_reason": "stop", "prompt_eval_duration": 250_000_000,
                          "eval_duration": 1_500_000_000, "eval_count": 12})
        content, t = w.parse_ollama_response(raw)
        self.assertEqual(content, "SHALL WE PLAY A GAME?")
        self.assertAlmostEqual(t["prompt_ms"], 250.0)
        self.assertAlmostEqual(t["gen_ms"], 1500.0)
        self.assertEqual(t["tokens"], 12)


class ResampleTests(unittest.TestCase):
    def test_downsample_preserves_tone_and_length(self):
        src = w.synth_tone(1000, 500, 0.5, sr=22050)
        out = w.resample(src, 22050, 8000)
        self.assertEqual(out.dtype, np.dtype("<i2"))
        self.assertAlmostEqual(len(out) / 4000, 1.0, places=2)
        spec = np.abs(np.fft.rfft(out[400:3600].astype(float)))
        peak_hz = np.argmax(spec) * 8000 / len(out[400:3600])
        self.assertAlmostEqual(peak_hz / 1000, 1.0, delta=0.05)
        # amplitude roughly preserved (within 15 %)
        self.assertAlmostEqual(np.max(np.abs(out[400:3600])) / (0.5 * 32767), 1.0, delta=0.15)

    def test_upsample_length(self):
        src = w.synth_tone(1000, 100, 0.5, sr=8000)
        out = w.resample(src, 8000, 16000)
        self.assertEqual(len(out), 1600)

    def test_wav_parser_tolerates_placeholder_sizes(self):
        samples = w.synth_tone(440, 20, 0.5, sr=22050)
        hdr = (b"RIFF" + b"\xff\xff\xff\xff" + b"WAVE" + b"fmt " + (16).to_bytes(4, "little")
               + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (22050).to_bytes(4, "little")
               + (44100).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
               + b"data" + b"\xff\xff\xff\xff")
        sr, got = w.parse_wav_bytes(hdr + samples.tobytes())
        self.assertEqual(sr, 22050)
        np.testing.assert_array_equal(got, samples)


class SynthTests(unittest.TestCase):
    def test_synth_tone_shape_and_amplitude(self):
        t = w.synth_tone(2600, 1000, 0.25)
        self.assertEqual(len(t), 8000)
        self.assertAlmostEqual(np.max(np.abs(t)) / (0.25 * 32767), 1.0, delta=0.01)

    def test_wink_frames(self):
        frames = w.wink_frames()
        self.assertEqual(len(frames), w.WINK_MS // 20)
        win = np.frombuffer(frames[len(frames) // 2], dtype="<i2")[:80]
        self.assertFalse(w.window_passes(win)[0])   # the wink must never pass the 2600 test


if __name__ == "__main__":
    unittest.main()


class TeardownTests(unittest.TestCase):
    """Generation counter: frames queued before a barge-in must never be sent afterwards."""

    def test_stale_generation_frames_are_dropped(self):
        import asyncio

        async def scenario():
            class _W:  # minimal StreamWriter stand-in
                def __init__(self): self.sent = []
                def write(self, b): self.sent.append(b)
                async def drain(self): pass
                def close(self): pass
                def get_extra_info(self, k): return None
            wr = _W()
            sess = w.CallSession(b"\x00" * 16, None, wr, w.Config())
            sess.playback_gen = 1
            await sess.enqueue([b"\x01" * w.FRAME_BYTES] * 5)     # gen 1 frames
            sess.playback_gen = 2                                  # barge-in happened
            dropped = sess.drain()
            self.assertEqual(dropped, 5)
            await sess.enqueue([b"\x02" * w.FRAME_BYTES] * 2)     # gen 2 frames
            # run the writer for a few ticks, then close
            task = asyncio.create_task(w.writer_task(sess))
            await asyncio.sleep(0.12)
            sess.closed = True
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, w.CallEnded):
                pass
            payloads = [f[3:] for f in wr.sent]
            self.assertTrue(all(p != b"\x01" * w.FRAME_BYTES for p in payloads), "stale frame leaked")
            self.assertEqual(sum(p == b"\x02" * w.FRAME_BYTES for p in payloads), 2)
            self.assertTrue(sess.outbound_q.empty())
        asyncio.run(scenario())


class AttractTests(unittest.TestCase):
    def _write_wav(self, path, sr, ch, samples):
        import wave
        with wave.open(path, "wb") as wf:
            wf.setnchannels(ch); wf.setsampwidth(2); wf.setframerate(sr)
            wf.writeframes(samples.astype("<i2").tobytes())

    def test_load_wav_file_mono_and_stereo(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        mono = w.synth_tone(1000, 100, 0.5, sr=22050)
        p1 = os.path.join(d, "m.wav"); self._write_wav(p1, 22050, 1, mono)
        sr, a = w.load_wav_file(p1)
        self.assertEqual(sr, 22050); self.assertEqual(len(a), len(mono))
        st = np.repeat(mono, 2)  # fake stereo interleave
        p2 = os.path.join(d, "s.wav"); self._write_wav(p2, 16000, 2, st)
        sr2, a2 = w.load_wav_file(p2)
        self.assertEqual(sr2, 16000); self.assertEqual(len(a2), len(mono))

    def test_load_attract_resamples_and_frames(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        src = w.synth_tone(440, 1000, 0.9, sr=22050)   # 1 s
        p = os.path.join(d, "a.wav"); self._write_wav(p, 22050, 1, src)
        frames = w.load_attract(p, gain=0.5)
        self.assertTrue(all(len(f) == w.FRAME_BYTES for f in frames))
        self.assertAlmostEqual(len(frames) * 0.02, 1.0, delta=0.05)   # ~1 s at 8 kHz
        # gain applied: peak of the loaded audio ≈ 0.5 * source peak
        loaded = np.frombuffer(b"".join(frames), dtype="<i2")
        self.assertLess(np.max(np.abs(loaded)), 0.7 * 32767)
        self.assertGreater(np.max(np.abs(loaded)), 0.3 * 32767)

    def test_load_attract_rejects_non16bit(self):
        import tempfile, os, wave
        d = tempfile.mkdtemp(); p = os.path.join(d, "b.wav")
        with wave.open(p, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(1); wf.setframerate(8000)
            wf.writeframes(b"\x80" * 800)
        with self.assertRaises(ValueError):
            w.load_attract(p)


class MovieQuestTests(unittest.TestCase):
    def test_unlocks_only_after_all_beats(self):
        q = w.MovieQuest()
        self.assertEqual(q.update("Hello WOPR, is Joshua there?"), "joshua")
        self.assertFalse(q.unlocked)
        self.assertEqual(q.update("Can we play a game?"), "play_game")
        self.assertEqual(q.update("Let's do global thermonuclear war."), "global_thermonuclear_war")
        self.assertFalse(q.unlocked)                       # 3/4, still locked
        self.assertEqual(q.update("The only winning move is not to play."), "winning_move")
        self.assertTrue(q.unlocked)                        # 4/4
        self.assertEqual(q.progress, "4/4")

    def test_order_independent_and_idempotent(self):
        q = w.MovieQuest()
        for t in ["winning move", "thermonuclear", "shall we play", "joshua"]:
            q.update(t)
        self.assertTrue(q.unlocked)
        # repeating a beat doesn't report it as newly-hit
        self.assertIsNone(q.update("joshua joshua"))

    def test_no_false_unlock_on_normal_chat(self):
        q = w.MovieQuest()
        for t in ["Hello.", "How are you?", "What games do you have?", "I like chess."]:
            q.update(t)
        self.assertFalse(q.unlocked)
        self.assertEqual(q.progress, "0/4")

    def test_reveal_text_substitutes_flag(self):
        reveal = w.FLAG_TEXT_DEFAULT.replace("{flag}", "JOSHUA FIVE BY FIVE")
        self.assertIn("JOSHUA FIVE BY FIVE", reveal)
        self.assertNotIn("{flag}", reveal)


class CallbackTests(unittest.TestCase):
    def test_sentinel_uuid_is_16_bytes(self):
        import uuid
        self.assertEqual(len(w.CALLBACK_UUID), 16)
        self.assertEqual(w.CALLBACK_UUID, uuid.UUID("ca11bac0-0000-4000-8000-000000000000").bytes)

    def test_gtw_beat_matches_callback_trigger_phrases(self):
        rx = w.MOVIE_BEATS["global_thermonuclear_war"]
        for t in ["let's play global thermonuclear war", "I choose thermonuclear war",
                  "nuclear war please"]:
            self.assertTrue(rx.search(t), t)
        self.assertIsNone(rx.search("how about chess"))

    def test_callback_config_defaults(self):
        c = w.Config()
        self.assertTrue(c.enable_callback)
        self.assertIn("2605", c.callback_dial)
        self.assertTrue(c.asterisk_bin.endswith("asterisk"))


class WinningMoveParaphraseTests(unittest.TestCase):
    def test_matches_paraphrases_and_canonical(self):
        rx = w.MOVIE_BEATS["winning_move"]
        for t in ["The only winning move is not to play.",
                  "The only way to win is by not making a move.",
                  "the winning move is not to move",
                  "not to play"]:
            self.assertTrue(rx.search(t), t)

    def test_no_false_match_on_normal_chat(self):
        rx = w.MOVIE_BEATS["winning_move"]
        for t in ["how about a game of chess", "I want to win the war", "let's play"]:
            self.assertIsNone(rx.search(t), t)


class WinCallbackTests(unittest.TestCase):
    def test_two_distinct_sentinels(self):
        import uuid
        self.assertEqual(w.CALLBACK_WIN_UUID, uuid.UUID("ca11bac1-0000-4000-8000-000000000000").bytes)
        self.assertNotEqual(w.CALLBACK_UUID, w.CALLBACK_WIN_UUID)

    def test_config_has_win_greeting_and_dial(self):
        c = w.Config()
        self.assertIn("2606", c.callback_win_dial)
        self.assertIn("WHY DID YOU ANSWER", c.callback_win_greeting)


class FreqEstimateTests(unittest.TestCase):
    def test_estimates_pitch_within_a_few_hz(self):
        for hz in (2480, 2560, 2600, 2635, 2710):
            x = w.synth_tone(hz, 300, 0.5)          # 300 ms, like the detector's hold window
            est = w.estimate_peak_hz(x)
            self.assertAlmostEqual(est, hz, delta=8, msg=f"{hz} -> {est:.1f}")

    def test_nan_on_tiny_input(self):
        import math
        self.assertTrue(math.isnan(w.estimate_peak_hz(np.zeros(16))))


class QuestCarryoverTests(unittest.TestCase):
    """A caller who hangs up mid-quest resumes their progress on the callback and can finish."""

    def test_callback_resumes_and_finishes(self):
        import asyncio

        async def scenario():
            class _W:
                def get_extra_info(self, k): return None
                def write(self, b): pass
                async def drain(self): pass
                def close(self): pass
            # caller had Joshua + play + thermonuclear war (3/4), then hung up -> pending handed over
            w._pending_quest = {"joshua", "play_game", "global_thermonuclear_war"}
            sess = w.CallSession(w.CALLBACK_UUID, None, _W(), w.Config())      # a sim callback
            self.assertEqual(sess.callback_kind, "sim")
            self.assertEqual(sess.quest.hit, {"joshua", "play_game", "global_thermonuclear_war"})
            self.assertEqual(w._pending_quest, set(), "pending should be consumed once")
            self.assertFalse(sess.quest.unlocked)                             # 3/4, not yet
            sess.quest.update("the only winning move is not to play")         # the last line
            self.assertTrue(sess.quest.unlocked)                             # flag on the callback

        asyncio.run(scenario())

    def test_normal_inbound_does_not_resume(self):
        import asyncio

        async def scenario():
            class _W:
                def get_extra_info(self, k): return None
                def write(self, b): pass
                async def drain(self): pass
                def close(self): pass
            w._pending_quest = {"joshua", "play_game", "global_thermonuclear_war"}
            sess = w.CallSession(b"\x22" * 16, None, _W(), w.Config())        # a fresh inbound call
            self.assertIsNone(sess.callback_kind)
            self.assertEqual(sess.quest.hit, set(), "inbound starts fresh")
            w._pending_quest = set()

        asyncio.run(scenario())


class ChessTests(unittest.TestCase):
    """SAN -> speech, and the caller-paced playthrough to checkmate."""

    def test_move_to_speech_covers_san_forms(self):
        self.assertEqual(w.move_to_speech("e4"), "PAWN TO E 4")
        self.assertEqual(w.move_to_speech("Nf3"), "KNIGHT TO F 3")
        self.assertEqual(w.move_to_speech("exd5"), "E PAWN TAKES D 5")
        self.assertEqual(w.move_to_speech("Bxf3"), "BISHOP TAKES F 3")
        self.assertEqual(w.move_to_speech("O-O"), "CASTLES KINGSIDE")
        self.assertEqual(w.move_to_speech("O-O-O"), "CASTLES QUEENSIDE")
        self.assertEqual(w.move_to_speech("Bb5+"), "BISHOP TO B 5, CHECK")
        self.assertEqual(w.move_to_speech("Nbd7"), "KNIGHT ON B TO D 7")
        self.assertEqual(w.move_to_speech("Rd8#"), "ROOK TO D 8, CHECKMATE")
        self.assertEqual(w.move_to_speech("e8=Q"), "PAWN TO E 8, PROMOTES TO QUEEN")

    def test_default_game_runs_to_mate_and_clears_state(self):
        plies = w.CHESS_MOVES_DEFAULT.split()
        self.assertTrue(w.CHESS_MOVES_DEFAULT.split()[-1].endswith("#"), "game must end in mate")

        class _S:
            def __init__(self): self.cfg = w.Config(); self.chess_idx = 0; self.in_chess = True
        s = _S()
        spoken = []
        while s.in_chess and s.chess_idx < len(plies) + 5:
            spoken.append(w.chess_next_line(s))
        self.assertEqual(len(spoken), len(plies))
        self.assertFalse(s.in_chess, "in_chess clears itself at checkmate")
        self.assertIn("CHECKMATE", spoken[-1])
        self.assertIn("WHITE WINS", spoken[-1])
        self.assertTrue(spoken[0].startswith("WHITE."), "white moves first")
        self.assertTrue(spoken[1].startswith("BLACK."))

    def test_session_starts_with_chess_off(self):
        import asyncio

        async def scenario():
            class _W:
                def get_extra_info(self, k): return None
                def write(self, b): pass
                async def drain(self): pass
                def close(self): pass
            sess = w.CallSession(b"\x33" * 16, None, _W(), w.Config())
            self.assertFalse(sess.in_chess)
            self.assertFalse(sess.chess_offered)
            self.assertEqual(sess.chess_idx, 0)

        asyncio.run(scenario())


class WakeExperienceTests(unittest.TestCase):
    """Wake-phrase → character routing, and per-experience isolation."""

    def test_wake_phrases_route_to_the_right_character(self):
        cases = {
            "please say joshua": "wopr",
            "noshua": "wopr",
            "i am looking for sarah connor": "skynet",
            "where is john connor": "skynet",
            "sara conor": "skynet",           # STT mishear
            "it is robocop": "robocop",
            "officer murphy": "robocop",
            "dead or alive you are coming with me": "robocop",
        }
        for text, key in cases.items():
            exp = w.match_experience(text)
            self.assertIsNotNone(exp, f"{text!r} should match")
            self.assertEqual(exp.key, key, f"{text!r} -> {exp.key}, want {key}")

    def test_non_wake_text_matches_nothing(self):
        for text in ["hello there", "what time is it", "the weather is nice", ""]:
            self.assertIsNone(w.match_experience(text), f"{text!r} should not match")

    def test_each_experience_has_distinct_voice_and_prompt(self):
        keys = [e.key for e in w.WAKE_EXPERIENCES]
        self.assertEqual(keys, ["wopr", "skynet", "robocop"])
        prompts = {e.system_prompt for e in w.WAKE_EXPERIENCES}
        self.assertEqual(len(prompts), 3, "each character has its own system prompt")
        self.assertTrue(all(e.greeting for e in w.WAKE_EXPERIENCES))

    def test_session_defaults_to_wopr(self):
        import asyncio

        async def scenario():
            class _W:
                def get_extra_info(self, k): return None
                def write(self, b): pass
                async def drain(self): pass
                def close(self): pass
            sess = w.CallSession(b"\x44" * 16, None, _W(), w.Config())
            self.assertEqual(sess.experience.key, "wopr")
            self.assertEqual(sess.idle_utterance, b"")

        asyncio.run(scenario())

    def test_wake_can_be_disabled(self):
        self.assertTrue(w.parse_args([]).enable_wake)
        self.assertFalse(w.parse_args(["--no-wake"]).enable_wake)
