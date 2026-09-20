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
        vec = float(w.window_energies(x)[w._BINS_K.index(26)])
        self.assertAlmostEqual(ref / vec, 1.0, places=6)

    def test_on_bin_tone_has_high_ratio_and_half_purity(self):
        ratio, e26, purity = w.window_ratio(tone(2600))
        self.assertGreater(ratio, 10)
        self.assertGreater(e26, w.MIN_BIN_ENERGY)
        self.assertAlmostEqual(purity, 0.5, places=2)

    def test_guard_tones_fail_ratio(self):
        # just outside the 2300–2900 band: the guard bins catch them
        for hz in (2100, 2200, 3000, 3100):
            ratio, _, _ = w.window_ratio(tone(hz))
            self.assertLess(ratio, w.RATIO_THRESHOLD, f"{hz} Hz ratio={ratio:.2f}")

    def test_drift_within_spec_passes(self):
        # the whole 2300–2900 band passes, at various phases (a real whistle wanders)
        for hz in (2320, 2450, 2570, 2585, 2600, 2615, 2630, 2640, 2750, 2880):
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
    """WOPR's quest, now driven by the generalized QuestState over WOPR_QUEST."""

    def wopr_quest(self):
        return w.QuestState(quest=w.WOPR_QUEST)

    def test_unlocks_only_after_all_beats(self):
        q = self.wopr_quest()
        self.assertEqual(q.update("Hello WOPR, is Joshua there?"), "joshua")
        self.assertFalse(q.unlocked)
        self.assertEqual(q.update("Can we play a game?"), "play_game")
        self.assertEqual(q.update("Let's do global thermonuclear war."), "global_thermonuclear_war")
        self.assertFalse(q.unlocked)                       # 3/4, still locked
        self.assertEqual(q.update("The only winning move is not to play."), "winning_move")
        self.assertTrue(q.unlocked)                        # 4/4
        self.assertEqual(q.progress, "4/4")

    def test_order_independent_and_idempotent(self):
        q = self.wopr_quest()
        for t in ["winning move", "thermonuclear", "shall we play", "joshua"]:
            q.update(t)
        self.assertTrue(q.unlocked)
        # repeating a beat doesn't report it as newly-hit
        self.assertIsNone(q.update("joshua joshua"))

    def test_no_false_unlock_on_normal_chat(self):
        q = self.wopr_quest()
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
            "the cake is a lie": "glados",
            "cake is a lie": "glados",
            "e t phone home": "et",
            "i want to phone home": "et",     # 'phone home' is E.T.'s only wake phrase
            "let me do a kick flip": "tonyhawk",
            "kickflip": "tonyhawk",
            "tony hawk": "tonyhawk",
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
        self.assertEqual(keys, ["wopr", "skynet", "robocop", "et", "tonyhawk",
                                "glados", "peewee", "hal", "greeter", "goodbye"])
        convo = [e for e in w.WAKE_EXPERIENCES if e.archetype in ("chat", "quest")]  # WOPR + chats
        prompts = {e.system_prompt for e in convo}
        self.assertEqual(len(prompts), 4, "wopr/skynet/robocop/et each have their own system prompt")
        self.assertTrue(all(e.greeting for e in convo))

    def test_glados_is_a_cutscene(self):
        g = w.match_experience("the cake is a lie")
        self.assertEqual(g.key, "glados")
        self.assertEqual(g.archetype, "cutscene")
        self.assertEqual(g.cutscene.clip, "song")
        self.assertEqual(g.cutscene.message_mode, "none")

    def test_greeter_is_an_operator_clip(self):
        g = w.match_experience("operator")
        self.assertEqual(g.key, "greeter")
        self.assertEqual(g.audio, "operator", "greeter plays the hints clip, not an LLM chat")

    def test_menu_greeting_is_the_default_idle_prompt(self):
        c = w.parse_args([])
        self.assertIn("GRRCON", c.menu_greeting.upper())
        self.assertIn("HACKER", c.menu_greeting.upper())

    def test_session_defaults_to_greeter_not_wopr(self):
        import asyncio

        async def scenario():
            class _W:
                def get_extra_info(self, k): return None
                def write(self, b): pass
                async def drain(self): pass
                def close(self): pass
            sess = w.CallSession(b"\x44" * 16, None, _W(), w.Config())
            self.assertEqual(sess.experience.key, "greeter", "default host is the greeter")
            self.assertEqual(sess.idle_utterance, b"")
            # a callback, however, is a WOPR feature
            cb = w.CallSession(w.CALLBACK_UUID, None, _W(), w.Config())
            self.assertEqual(cb.experience.key, "wopr")

        asyncio.run(scenario())

    def test_operator_summons_the_greeter(self):
        for t in ["operator", "operator please", "get me the operator", "the operator", "main menu"]:
            m = w.match_experience(t)
            self.assertIsNotNone(m, f"{t!r} should match")
            self.assertEqual(m.key, "greeter", f"{t!r} -> {m.key}")

    def test_menu_greeting_words_do_not_self_trigger_the_greeter(self):
        # the menu greeting bleeds into the mouthpiece; none of its words may summon the operator
        for t in ["how may i help you", "hello hacker", "i need help"]:
            self.assertIsNone(w.match_experience(t), f"{t!r} must NOT match (menu sidetone)")
        # "welcome to GrrCon" DOES match on purpose: GrrCon is Pee-wee's secret word. The sidetone
        # guard (idle VAD paused while the menu speaks) is what stops the menu from triggering it.
        self.assertEqual(w.match_experience("welcome to grrcon").key, "peewee")


class GameStatsTests(unittest.TestCase):
    """Per-call game categorization and the running aggregate DB."""

    def _sess(self):
        class _W:
            def get_extra_info(self, k): return None
            def write(self, b): pass
            async def drain(self): pass
            def close(self): pass
        return w.CallSession(b"\x66" * 16, None, _W(), w.Config())

    def test_metrics_categorize_games(self):
        import asyncio

        async def scenario():
            s = self._sess()
            s.note_game("et", reached=True)
            s.note_game("et", turn=True)
            s.note_game("wopr", reached=True)
            s.note_game("wopr", won=True)
            s.note_game("greeter", reached=True, clip=True)   # utility, excluded
            m = s.metrics("eof")
            self.assertEqual(m["games_played"], ["et", "wopr"])
            self.assertIn("greeter", m["games"])                # tracked
            self.assertNotIn("greeter", m["games_played"])      # but not counted as a game
            self.assertTrue(m["reached_chat"])
            self.assertTrue(m["games"]["wopr"]["won"])

        asyncio.run(scenario())

    def test_aggregate_db_accumulates(self):
        import asyncio, json, tempfile, os

        async def scenario():
            s = self._sess()
            s.note_game("tonyhawk", reached=True)
            s.note_game("tonyhawk", turn=True)
            s.note_game("tonyhawk", turn=True)
            m = s.metrics("eof")
            path = tempfile.mktemp(suffix=".json")
            try:
                w.update_stats_db(path, m, s.log)
                w.update_stats_db(path, m, s.log)
                db = json.load(open(path))
            finally:
                if os.path.exists(path):
                    os.remove(path)
            self.assertEqual(db["total_calls"], 2)
            self.assertEqual(db["games"]["tonyhawk"]["sessions"], 2)
            self.assertEqual(db["games"]["tonyhawk"]["turns"], 4)

        asyncio.run(scenario())

    def test_wake_can_be_disabled(self):
        self.assertTrue(w.parse_args([]).enable_wake)
        self.assertFalse(w.parse_args(["--no-wake"]).enable_wake)


class GameFrameworkTests(unittest.TestCase):
    """Spec 002 MVP: archetypes, generalized QuestState, and startup validation."""

    def test_queststate_none_never_unlocks(self):
        q = w.QuestState(quest=None)
        self.assertIsNone(q.update("joshua thermonuclear winning move"))
        self.assertFalse(q.unlocked)
        self.assertEqual(q.progress, "0/0")

    def test_queststate_any_order(self):
        import re
        quest = w.Quest(objectives=(
            w.Objective("a", re.compile(r"alpha", re.I)),
            w.Objective("b", re.compile(r"bravo", re.I)),
        ), ordered=False)
        q = w.QuestState(quest=quest)
        self.assertEqual(q.update("bravo first"), "b")   # out of list order is fine
        self.assertFalse(q.unlocked)
        self.assertEqual(q.update("then alpha"), "a")
        self.assertTrue(q.unlocked)

    def test_queststate_ordered_requires_sequence(self):
        import re
        quest = w.Quest(objectives=(
            w.Objective("a", re.compile(r"alpha", re.I)),
            w.Objective("b", re.compile(r"bravo", re.I)),
        ), ordered=True)
        q = w.QuestState(quest=quest)
        self.assertIsNone(q.update("bravo only"))  # b can't be hit before a
        self.assertFalse(q.unlocked)
        self.assertEqual(q.update("alpha"), "a")
        self.assertEqual(q.update("bravo"), "b")
        self.assertTrue(q.unlocked)

    def test_queststate_ordered_accepts_whole_sequence_in_one_breath(self):
        import re
        quest = w.Quest(objectives=(
            w.Objective("a", re.compile(r"alpha", re.I)),
            w.Objective("b", re.compile(r"bravo", re.I)),
            w.Objective("c", re.compile(r"charlie", re.I)),
        ), ordered=True)
        q = w.QuestState(quest=quest)
        self.assertEqual(q.update("alpha bravo"), "b")      # a then b, both in one utterance
        self.assertFalse(q.unlocked)
        self.assertIsNone(q.update("alpha again"))          # no new objective
        self.assertEqual(q.update("charlie"), "c")
        self.assertTrue(q.unlocked)
        q2 = w.QuestState(quest=quest)
        self.assertIsNone(q2.update("bravo charlie"))       # skipping a: nothing counts
        self.assertEqual(q2.progress, "0/3")

    def test_archetypes(self):
        by_key = {e.key: e for e in w.WAKE_EXPERIENCES}
        self.assertEqual(by_key["wopr"].archetype, "quest")
        self.assertIs(by_key["wopr"].quest, w.WOPR_QUEST)
        # US2: the three chat characters are now quests with their own flags
        self.assertEqual(by_key["skynet"].archetype, "quest")
        self.assertEqual(by_key["robocop"].archetype, "quest")
        self.assertEqual(by_key["et"].archetype, "quest")
        # US3: GLaDOS and Tony Hawk are cutscenes (play-and-close)
        self.assertEqual(by_key["tonyhawk"].archetype, "cutscene")
        self.assertEqual(by_key["glados"].archetype, "cutscene")
        # utilities are not games
        self.assertFalse(w.is_game("greeter"))
        self.assertFalse(w.is_game("goodbye"))

    def test_minimal_game_definitions_validate(self):
        import re, logging
        log = w.get_logger("test")
        # a valid quest game and a valid cutscene game raise no invalid count
        good_quest = w.Experience("q", wake=re.compile("q"),
                                  quest=w.Quest(objectives=(w.Objective("x", re.compile("x")),), flag="F"))
        good_cut = w.Experience("c", wake=re.compile("c"), cutscene=w.Cutscene(clip="song"))
        self.assertEqual(good_quest.archetype, "quest")
        self.assertEqual(good_cut.archetype, "cutscene")
        # the shipped roster is all valid
        self.assertEqual(w.validate_games(log), 0)

    def test_shipped_roster_has_no_invalid_games(self):
        self.assertEqual(w.validate_games(w.get_logger("test")), 0)


class CutsceneTests(unittest.TestCase):
    """Spec 002 US3: cutscene play-and-close, end-call, and completion recording."""

    def _sess(self, writes=None):
        class _W:
            def get_extra_info(self, k): return None
            def write(self, b):
                if writes is not None:
                    writes.append(b)
            async def drain(self): pass
            def close(self): pass
        return w.CallSession(b"\x77" * 16, None, _W(), w.Config())

    def test_end_call_sends_terminate_frame(self):
        import asyncio

        async def scenario():
            writes = []
            sess = self._sess(writes)
            await w.end_call(sess)
            self.assertIn(w.encode_frame(w.FrameType.TERMINATE), writes)

        asyncio.run(scenario())

    def test_end_call_safe_when_writer_broken(self):
        import asyncio

        async def scenario():
            class _Broken:
                def get_extra_info(self, k): return None
                def write(self, b): raise ConnectionError("gone")
                async def drain(self): pass
                def close(self): pass
            sess = w.CallSession(b"\x78" * 16, None, _Broken(), w.Config())
            await w.end_call(sess)   # must not raise

        asyncio.run(scenario())

    def test_completed_recorded_in_metrics(self):
        import asyncio

        async def scenario():
            sess = self._sess()
            sess.note_game("glados", reached=True, clip=True, completed=True)
            m = sess.metrics("eof")
            self.assertTrue(m["games"]["glados"]["completed"])
            self.assertIn("glados", m["games_played"])

        asyncio.run(scenario())

    def test_aggregate_counts_completions(self):
        import asyncio, json, tempfile, os

        async def scenario():
            sess = self._sess()
            sess.note_game("glados", reached=True, clip=True, completed=True)
            m = sess.metrics("eof")
            path = tempfile.mktemp(suffix=".json")
            try:
                w.update_stats_db(path, m, sess.log)
                w.update_stats_db(path, m, sess.log)
                db = json.load(open(path))
            finally:
                if os.path.exists(path):
                    os.remove(path)
            self.assertEqual(db["games"]["glados"]["completions"], 2)

        asyncio.run(scenario())

    def test_tonyhawk_cutscene_has_generated_message_and_fallback(self):
        cs = {e.key: e for e in w.WAKE_EXPERIENCES}["tonyhawk"].cutscene
        self.assertEqual(cs.message_mode, "generated")
        self.assertIn("PATRICK GARRITY", cs.message_text)   # fallback line present
        self.assertTrue(cs.message_prompt)                  # a generation seed exists

    def test_arcade_mode_cutscenes_return_to_menu_goodbye_hangs_up(self):
        bk = {e.key: e for e in w.WAKE_EXPERIENCES}
        # games return to the menu (no hang-up)
        self.assertFalse(bk["glados"].cutscene.end_call)
        self.assertFalse(bk["tonyhawk"].cutscene.end_call)
        # only the explicit goodbye hangs up
        self.assertTrue(bk["goodbye"].cutscene.end_call)

    def test_menu_return_phrases(self):
        for t in ["take me to the main menu", "go back", "next game", "another game", "start over"]:
            self.assertTrue(w.MENU_RETURN_RE.search(t), f"{t!r} should return to menu")
        for t in ["global thermonuclear war", "joshua", "the cake is a lie"]:
            self.assertIsNone(w.MENU_RETURN_RE.search(t), f"{t!r} must NOT be a menu-return")


class QuestWinTests(unittest.TestCase):
    """Spec 002 US2: every quest game is winnable from its declared objectives; WOPR unchanged."""

    def _sess(self):
        class _W:
            def get_extra_info(self, k): return None
            def write(self, b): pass
            async def drain(self): pass
            def close(self): pass
        return w.CallSession(b"\x88" * 16, None, _W(), w.Config())

    def by_key(self):
        return {e.key: e for e in w.WAKE_EXPERIENCES}

    def test_wopr_still_unlocks_from_the_four_beats_with_cfg_flag(self):
        q = w.QuestState(quest=w.WOPR_QUEST)
        for t in ["joshua", "let's play a game", "global thermonuclear war", "the only winning move is not to play"]:
            q.update(t)
        self.assertTrue(q.unlocked)
        # WOPR leaves reveal/flag empty so the env-overridable cfg values keep applying
        self.assertEqual(w.WOPR_QUEST.flag, "")
        self.assertEqual(w.WOPR_QUEST.reveal, "")
        self.assertIn("JOSHUA FIVE BY FIVE", w.FLAG_TEXT_DEFAULT.replace("{flag}", w.FLAG_DEFAULT))

    def test_skynet_unlocks_any_order_with_mishears(self):
        q = w.QuestState(quest=w.SKYNET_QUEST)
        self.assertEqual(q.update("judgement day is coming"), "judgment_day")   # STT spelling
        self.assertEqual(q.update("pasta la vista baby"), "hasta_la_vista")     # mishear
        self.assertEqual(w.QuestState(quest=w.SKYNET_QUEST).update("Asta LaVista."), "hasta_la_vista")
        self.assertFalse(q.unlocked)
        self.assertEqual(q.update("all be back"), "ill_be_back")                # mishear
        self.assertTrue(q.unlocked)
        q2 = w.QuestState(quest=w.SKYNET_QUEST)
        for t in ["I'll be back.", "Hasta la vista, baby.", "Judgment Day."]:
            q2.update(t)
        self.assertTrue(q2.unlocked)
        # the date counts too, including the mishears heard on a real call
        for t in ["August 29, 1997", "April 29, 1997.", "the twenty ninth", "2:14 a.m.", "1997"]:
            self.assertEqual(w.QuestState(quest=w.SKYNET_QUEST).update(t), "judgment_day", t)

    def test_robocop_requires_directives_in_order(self):
        q = w.QuestState(quest=w.ROBOCOP_QUEST)
        self.assertIsNone(q.update("uphold the law"))                # directive three first: no
        self.assertEqual(q.progress, "0/3")
        self.assertEqual(q.update("serve the public trust"), "serve")
        self.assertIsNone(q.update("uphold the law"))                # still not two
        self.assertEqual(q.update("protect the innocent"), "protect")
        self.assertEqual(q.update("uphold the law"), "uphold")
        self.assertTrue(q.unlocked)
        # the whole recitation in one breath also counts
        q2 = w.QuestState(quest=w.ROBOCOP_QUEST)
        self.assertEqual(q2.update("serve the public trust, protect the innocent, uphold the law"), "uphold")
        self.assertTrue(q2.unlocked)

    def test_et_unlocks_from_the_communicator_parts(self):
        q = w.QuestState(quest=w.ET_QUEST)
        self.assertEqual(q.update("here is a speak n spell"), "speak_and_spell")   # mishear
        self.assertEqual(w.QuestState(quest=w.ET_QUEST).update("Speaking spell"), "speak_and_spell")
        self.assertEqual(q.update("I brought umbrellas"), "umbrella")
        self.assertFalse(q.unlocked)
        self.assertEqual(q.update("and a turntable"), "record_player")          # synonym
        self.assertTrue(q.unlocked)

    def test_no_false_unlock_on_normal_chat(self):
        for quest in (w.SKYNET_QUEST, w.ROBOCOP_QUEST, w.ET_QUEST):
            q = w.QuestState(quest=quest)
            for t in ["Hello.", "How are you?", "What is your name?", "Tell me about the weather.",
                      "I like movies.", "Goodbye."]:
                q.update(t)
            self.assertFalse(q.unlocked, quest.flag)
            self.assertEqual(len(q.hit), 0, quest.flag)

    def test_every_shipped_quest_has_flag_reveal_and_feedback_lines(self):
        for key in ("skynet", "robocop", "et"):
            quest = self.by_key()[key].quest
            self.assertTrue(quest.flag, key)
            self.assertIn("{flag}", quest.reveal, key)
            self.assertTrue(quest.objectives, key)
            for o in quest.objectives:
                self.assertTrue(o.response, f"{key}:{o.name} needs a canned response")
                self.assertTrue(o.label, f"{key}:{o.name} needs a label")
        self.assertEqual(w.validate_games(w.get_logger("test")), 0)

    def test_flag_tally_counts_every_quest(self):
        import asyncio

        async def scenario():
            s = self._sess()
            n = sum(1 for e in w.WAKE_EXPERIENCES if e.quest)
            self.assertEqual(n, 4)
            s.note_game("wopr", won=True)
            self.assertTrue(w.flag_tally(s).startswith(f"1 OF {n} FLAGS"))
            for k in ("skynet", "robocop", "et"):
                s.note_game(k, won=True)
            self.assertTrue(w.flag_tally(s).startswith(f"ALL {n} FLAGS"))

        asyncio.run(scenario())

    def test_wopr_funnel_survives_switching_games(self):
        """Arcade: win WOPR, go back to the menu, play RoboCop -> per-call won/points still WOPR's."""
        import asyncio

        async def scenario():
            s = self._sess()
            s.experience = w.WOPR_EXPERIENCE
            s.sync_quest()
            for t in ["joshua", "play a game", "thermonuclear war", "the only winning move is not to play"]:
                s.quest.update(t)
            self.assertTrue(s.quest.unlocked)
            s.note_game("wopr", reached=True, won=True)
            s.experience = w.ROBOCOP_EXPERIENCE       # menu return, then summon RoboCop
            s.sync_quest()
            self.assertIs(s.quest.quest, w.ROBOCOP_QUEST)
            self.assertEqual(s.quest.progress, "0/3")  # fresh progress for the new game
            s.note_game("robocop", reached=True)
            m = s.metrics("eof")
            self.assertTrue(m["won"])                  # the WOPR funnel signal
            self.assertEqual(m["points"], 4)
            self.assertTrue(m["games"]["wopr"]["won"])
            self.assertFalse(m["games"]["robocop"]["won"])
            self.assertEqual(m["games_played"], ["robocop", "wopr"])

        asyncio.run(scenario())

    def test_whistle_bargein_resets_progress_but_keeps_earned_flags(self):
        """FR-010 / spec US2 scenario 4: a whistle mid-quest restarts as WOPR with fresh progress;
        flags already earned this call are kept (arcade)."""
        import asyncio

        async def scenario():
            s = self._sess()
            s.experience = w.ROBOCOP_EXPERIENCE
            s.sync_quest()
            s.quest.update("serve the public trust, protect the innocent, uphold the law")
            self.assertTrue(s.quest.unlocked)
            s.note_game("robocop", reached=True, won=True)
            # brain_task's barge-in reset
            s.experience = w.WOPR_EXPERIENCE
            s.quest = w.QuestState(quest=s.experience.quest)
            self.assertEqual(s.quest.progress, "0/4")
            self.assertFalse(s.quest.unlocked)
            self.assertTrue(s.games["robocop"]["won"])
            self.assertTrue(w.flag_tally(s).startswith("1 OF 4 FLAGS"))

        asyncio.run(scenario())


class PeeweeCutsceneTests(unittest.TestCase):
    """'Secret word' -> the Pee-wee scream clip -> Tequila -> back to the menu (a two-clip cutscene)."""

    def test_secret_word_summons_peewee(self):
        for t in ["grrcon", "GrrCon!", "grr con", "gercon", "girl con", "grr khan", "gurcon",
                  "grrr con", "welcome to grrcon", "I think I like Ghercon", "gher con"]:
            exp = w.match_experience(t)
            self.assertIsNotNone(exp, t)
            self.assertEqual(exp.key, "peewee", t)
        # the old wake words are gone: "secret word" is the hint, not the trigger
        for t in ["what's the secret word", "pee-wee", "mecca lecca hi", "grand canyon", "green"]:
            m = w.match_experience(t)
            self.assertTrue(m is None or m.key != "peewee", f"{t!r} must not summon peewee")

    def test_peewee_is_a_two_clip_cutscene_that_returns_to_the_menu(self):
        exp = {e.key: e for e in w.WAKE_EXPERIENCES}["peewee"]
        self.assertEqual(exp.archetype, "cutscene")
        self.assertEqual(exp.cutscene.clips, ("peewee-secret-word", "tequila"))
        self.assertEqual(exp.cutscene.message_mode, "none")
        self.assertFalse(exp.cutscene.end_call)
        self.assertTrue(w.is_game("peewee"))
        self.assertEqual(w.validate_games(w.get_logger("test")), 0)

    def test_run_cutscene_plays_clips_in_order_and_skips_missing(self):
        import asyncio

        async def scenario():
            class _W:
                def get_extra_info(self, k): return None
                def write(self, b): pass
                async def drain(self): pass
                def close(self): pass
            sess = w.CallSession(b"\x99" * 16, None, _W(), w.Config())
            sess.experience = {e.key: e for e in w.WAKE_EXPERIENCES}["peewee"]
            played = []

            async def fake_play(frames, kind):
                played.append(kind)
            sess.play = fake_play
            saved = dict(w.CLIP_FRAMES)
            try:
                w.CLIP_FRAMES.clear()
                w.CLIP_FRAMES["tequila"] = [b"\x00" * w.FRAME_BYTES]   # only the second clip is on disk
                await w.run_cutscene(sess)
            finally:
                w.CLIP_FRAMES.clear()
                w.CLIP_FRAMES.update(saved)
            self.assertEqual(played, ["cutscene:tequila"])                # missing scream skipped, not fatal
            self.assertTrue(sess.games["peewee"]["completed"])
            # an optional <key>-outro.wav plays after the clips, by filename convention alone
            played.clear()
            try:
                w.CLIP_FRAMES["peewee-outro"] = [b"\x00" * w.FRAME_BYTES]
                w.CLIP_FRAMES["tequila"] = [b"\x00" * w.FRAME_BYTES]
                await w.run_cutscene(sess)
            finally:
                w.CLIP_FRAMES.clear()
                w.CLIP_FRAMES.update(saved)
            self.assertEqual(played, ["cutscene:tequila", "outro:peewee"])

        asyncio.run(scenario())


class AnalyzeReportTests(unittest.TestCase):
    """Spec 002 US4: analyze.py separates quest wins from cutscene completions, legacy-safe."""

    def test_per_game_table_has_wins_and_completions_and_tolerates_legacy(self):
        import analyze
        legacy = {"call": "a", "callback": None, "whistled": True, "won": True, "points": 4,
                  "beats": ["joshua", "play_game", "global_thermonuclear_war", "winning_move"],
                  "duration_ms": 1000, "turns": 3}                                   # pre-framework record
        quest = {"call": "b", "callback": None, "whistled": False, "won": True, "points": 4,
                 "beats": [], "duration_ms": 1000, "turns": 4,
                 "games": {"wopr": {"reached": True, "turns": 4, "won": True, "clip": False}}}   # no 'completed' key
        cut = {"call": "c", "callback": None, "whistled": False, "won": False, "points": 0,
               "beats": [], "duration_ms": 500, "turns": 0,
               "games": {"glados": {"reached": True, "turns": 0, "won": False, "clip": True,
                                    "completed": True}}}
        text, summary = analyze.build_report([legacy, quest, cut], [])
        self.assertIn("compl", text)
        lines = text.splitlines()
        hdr = next(i for i, ln in enumerate(lines)
                   if ln.strip().startswith("game") and "compl" in ln and "players" in ln)
        table = []
        for ln in lines[hdr + 1:]:            # the table rows end at the first blank line
            if not ln.strip():
                break
            table.append(ln)
        rows = {ln.split()[0]: ln for ln in table}
        self.assertIn("WOPR", rows)
        self.assertIn("GLaDOS", rows)
        wopr_cols = rows["WOPR"].split()
        glados_cols = rows["GLaDOS"].split()
        # columns: ... wins compl clips  (last three numbers)
        self.assertEqual(wopr_cols[-3:], ["1", "0", "0"])
        self.assertEqual(glados_cols[-3:], ["0", "1", "1"])


class TrunkSeizeTests(unittest.TestCase):
    """The gate: a picked-up line is dead until 2600 Hz seizes the trunk; every whistle drops the
    caller at the menu; an un-whistled line is hung up after a timeout."""

    def _sess(self, cfg=None, writes=None, uuid=b"\xaa" * 16):
        class _W:
            def get_extra_info(self, k): return None
            def write(self, b):
                if writes is not None:
                    writes.append(b)
            async def drain(self): pass
            def close(self): pass
        return w.CallSession(uuid, None, _W(), cfg or w.Config())

    def _whistle(self, sess, n=None):
        for i in range(n or w.HOLD_WINDOWS):
            sess.process_inbound_window(tone(2600, phase=i))

    def test_line_starts_dead_and_the_whistle_seizes_it(self):
        import asyncio

        async def scenario():
            sess = self._sess()
            self.assertFalse(sess.seized)
            self.assertIsNone(sess.next_idle_frame(), "dead line: no menu before the whistle")
            self._whistle(sess)
            self.assertTrue(sess.seized)
            self.assertTrue(sess.whistle_pending)
            self.assertTrue(sess.detected_evt.is_set())
            self.assertEqual(sess.experience.key, "greeter", "the whistle no longer summons WOPR")
            self.assertIsNotNone(sess.t_first_detect_ms)
            self.assertTrue(sess.metrics("eof")["seized"])

        asyncio.run(scenario())

    def test_menu_plays_only_once_seized_and_idle(self):
        import asyncio

        async def scenario():
            saved = w.ATTRACT_FRAMES
            w.ATTRACT_FRAMES = [b"\x01" * w.FRAME_BYTES, w._ATTRACT_SILENCE]
            try:
                sess = self._sess()
                self.assertIsNone(sess.next_idle_frame())
                sess.seized = True
                self.assertEqual(sess.next_idle_frame(), b"\x01" * w.FRAME_BYTES)
                sess.set_state(w.CallState.SPEAKING)
                self.assertIsNone(sess.next_idle_frame(), "not idle: no menu")
            finally:
                w.ATTRACT_FRAMES = saved

        asyncio.run(scenario())

    def test_wake_phrases_are_ignored_before_the_seize(self):
        import asyncio

        async def scenario():
            rng = np.random.default_rng(7)
            quiet = lambda: (rng.standard_normal(80) * 20).astype("<i2")  # noqa: E731
            loud = lambda: (rng.standard_normal(80) * 3000).astype("<i2")  # noqa: E731

            def speak(sess):
                for _ in range(30):
                    sess.process_inbound_window(quiet())
                for _ in range(60):
                    sess.process_inbound_window(loud())
                for _ in range(120):
                    sess.process_inbound_window(quiet())
            dead = self._sess()
            speak(dead)
            self.assertFalse(dead.idle_utt_evt.is_set(), "dead line: the wake listener must not hear")
            live = self._sess()
            live.seized = True
            speak(live)
            self.assertTrue(live.idle_utt_evt.is_set(), "seized: the wake listener hears")

        asyncio.run(scenario())

    def test_gate_can_be_disabled_and_callbacks_skip_it(self):
        import asyncio

        async def scenario():
            self.assertTrue(self._sess(w.Config(enable_seize=False)).seized)
            self.assertTrue(self._sess(uuid=w.CALLBACK_UUID).seized)
            self.assertTrue(w.parse_args([]).enable_seize)
            self.assertFalse(w.parse_args(["--no-seize"]).enable_seize)
            self.assertEqual(w.parse_args([]).seize_timeout, 90.0)
            self.assertEqual(w.parse_args(["--seize-timeout", "0"]).seize_timeout, 0.0)

        asyncio.run(scenario())

    def test_watchdog_hangs_up_an_unwhistled_line(self):
        import asyncio

        async def scenario():
            writes = []
            sess = self._sess(w.Config(seize_timeout=0.01), writes)
            with self.assertRaises(w.CallEnded) as cm:
                await w.seize_watchdog(sess)
            self.assertEqual(str(cm.exception), "no_seize")
            self.assertIn(w.encode_frame(w.FrameType.TERMINATE), writes)
            # a seized line is left alone
            sess2 = self._sess(w.Config(seize_timeout=0.01))
            sess2.seized = True
            await w.seize_watchdog(sess2)           # returns, no raise
            # timeout 0 = never
            await w.seize_watchdog(self._sess(w.Config(seize_timeout=0)))

        asyncio.run(scenario())

    def test_whistle_once_ignores_later_whistles_by_default(self):
        import asyncio

        async def scenario():
            sess = self._sess()
            self._whistle(sess)
            self.assertTrue(sess.seized)
            sess.whistle_pending = False
            sess.detected_evt.clear()
            self._whistle(sess, n=w.HOLD_WINDOWS + w.REARM_WINDOWS + 5)    # a second whistle
            self.assertFalse(sess.whistle_pending, "voice only after the seize")
            self.assertEqual(sess.detector.fires, 1)
            always = self._sess(w.Config(whistle_once=False))
            self._whistle(always); always.whistle_pending = False; always.detected_evt.clear()
            for i in range(w.REARM_WINDOWS + 2):
                always.process_inbound_window(np.zeros(80, dtype="<i2"))
            self._whistle(always)
            self.assertTrue(always.whistle_pending, "--whistle-always: a later whistle returns to the menu")
            self.assertTrue(w.parse_args([]).whistle_once)
            self.assertFalse(w.parse_args(["--whistle-always"]).whistle_once)

        asyncio.run(scenario())

    def test_operator_hints_no_longer_route_the_whistle_to_wopr(self):
        self.assertNotIn("WHISTLE TWENTY SIX HUNDRED HERTZ, OR SAY JOSHUA", w.OPERATOR_HINTS_DEFAULT)
        self.assertIn("JOSHUA", w.OPERATOR_HINTS_DEFAULT)
        self.assertIn("MAIN MENU", w.OPERATOR_HINTS_DEFAULT)


class ClipConventionTests(unittest.TestCase):
    """Audio drops in by filename: clip_for() resolves the first loaded key; SIT tones synthesize."""

    def test_clip_for_prefers_specific_then_shared_then_none(self):
        saved = dict(w.CLIP_FRAMES)
        try:
            w.CLIP_FRAMES.clear()
            self.assertIsNone(w.clip_for("robocop-fanfare", "fanfare"))
            w.CLIP_FRAMES["fanfare"] = [b"\x01" * w.FRAME_BYTES]
            self.assertEqual(w.clip_for("robocop-fanfare", "fanfare"), w.CLIP_FRAMES["fanfare"])
            w.CLIP_FRAMES["robocop-fanfare"] = [b"\x02" * w.FRAME_BYTES]
            self.assertEqual(w.clip_for("robocop-fanfare", "fanfare"), w.CLIP_FRAMES["robocop-fanfare"])
            self.assertIsNone(w.clip_for("", "nope"))     # empty keys are skipped
        finally:
            w.CLIP_FRAMES.clear()
            w.CLIP_FRAMES.update(saved)

    def test_sit_tones_are_three_rising_tones(self):
        frames = w.sit_frames()
        self.assertTrue(all(len(f) == w.FRAME_BYTES for f in frames))
        self.assertGreater(len(frames) * 20, 3 * 330)          # >= ~1 s
        pcm = np.frombuffer(b"".join(frames), dtype="<i2")
        seg = len(pcm) // 4
        hz = [w.estimate_peak_hz(pcm[i * seg:(i + 1) * seg], lo=800.0, hi=2000.0) for i in range(3)]
        self.assertTrue(hz[0] < hz[1] < hz[2], hz)


class SpeechBargeInTests(unittest.TestCase):
    """Talking over a clip stops it; sidetone does not. The interrupting words become the utterance."""

    def _sess(self):
        class _W:
            def get_extra_info(self, k): return None
            def write(self, b): pass
            async def drain(self): pass
            def close(self): pass
        return w.CallSession(b"\xbb" * 16, None, _W(), w.Config())

    def _noise(self, rng, level, n):
        return [(rng.standard_normal(80) * level).astype("<i2") for _ in range(n)]

    def test_sidetone_never_fires_and_speech_does(self):
        rng = np.random.default_rng(11)
        det = w.BargeDetector()
        # measured sidetone of the loudest clip: ~500 typical, spikes to ~1400
        for win in self._noise(rng, 500, 200) + self._noise(rng, 1400, 40) + self._noise(rng, 500, 100):
            self.assertFalse(det.update(win))
        # the caller talking over it (~3000 RMS) for 300 ms
        fired = [det.update(win) for win in self._noise(rng, 3000, w.BARGE_LOUD_WINDOWS + 2)]
        self.assertEqual(sum(fired), 1)

    def test_bursty_speech_fires(self):
        """Real speech clears the bar in only ~half its 10 ms windows; that must still count."""
        rng = np.random.default_rng(15)
        det = w.BargeDetector()
        for win in self._noise(rng, 400, 50):
            det.update(win)
        fired = []
        for k in range(w.BARGE_SPAN_WINDOWS):
            fired.append(det.update(self._noise(rng, 3000 if k % 2 == 0 else 600, 1)[0]))
        self.assertEqual(sum(fired), 1)

    def test_loud_playback_raises_the_bar(self):
        """A hot clip's sidetone (measured ~0.17 × outbound) must not trip the detector; real speech over
        it still does. The turret intro plays at ~16000 RMS -> sidetone ~2700, above BARGE_RMS_MIN."""
        rng = np.random.default_rng(14)
        det = w.BargeDetector()
        for win in self._noise(rng, 2700, 60):                 # sidetone of a 16000-RMS clip
            self.assertFalse(det.update(win, sidetone=16000.0))
        fired = [det.update(win, sidetone=16000.0) for win in self._noise(rng, 8000, w.BARGE_LOUD_WINDOWS + 2)]
        self.assertEqual(sum(fired), 1)                        # the caller shouting over it
        det = w.BargeDetector()                                # silence out: the floor is the only bar
        self.assertTrue(any(det.update(win, sidetone=0.0) for win in self._noise(rng, 2700, w.BARGE_LOUD_WINDOWS + 2)))

    def test_short_shout_does_not_fire(self):
        rng = np.random.default_rng(12)
        det = w.BargeDetector()
        for win in self._noise(rng, 400, 50):
            det.update(win)
        self.assertFalse(any(det.update(win) for win in self._noise(rng, 3000, 15)))   # 150 ms

    def test_bargein_stops_playback_and_seeds_the_utterance(self):
        import asyncio

        async def scenario():
            sess = self._sess()
            sess.set_state(w.CallState.SPEAKING)
            frames = [b"\x00" * w.FRAME_BYTES] * 200
            await sess.enqueue(frames)
            self.assertEqual(sess.outbound_q.qsize(), 200)
            sess.bargeable = True
            sess.utterance_evt.set()                      # an unread utterance from before the switch
            rng = np.random.default_rng(13)
            for win in self._noise(rng, 3000, w.BARGE_LOUD_WINDOWS + 1):
                sess.process_inbound_window(win)
            self.assertTrue(sess.interrupted)
            self.assertFalse(sess.utterance_evt.is_set(), "a stale utterance must not replay after a barge-in")
            self.assertEqual(sess.outbound_q.qsize(), 0, "queued playback drained")
            self.assertIs(sess.state, w.CallState.CAPTURING)
            self.assertTrue(sess.vad.in_speech, "VAD seeded mid-utterance")
            self.assertGreater(len(sess.vad.buf), 0)
            # a queued play() is skipped while the barge-in is pending
            await sess.play(frames, "would_be_skipped")
            self.assertEqual(sess.outbound_q.qsize(), 0)

        asyncio.run(scenario())

    def test_short_plays_and_no_barge_plays_are_not_interruptible(self):
        import asyncio

        async def scenario():
            sess = self._sess()
            sess.set_state(w.CallState.SPEAKING)
            sess.drain()

            async def drainer():
                while True:
                    await asyncio.sleep(0.001)
                    while not sess.outbound_q.empty():
                        sess.outbound_q.get_nowait(); sess.outbound_q.task_done()
            t = asyncio.create_task(drainer())
            seen = []
            orig_reset = sess.barge.reset
            sess.barge.reset = lambda: (seen.append(True), orig_reset())[1]
            await sess.play([b"\x00" * w.FRAME_BYTES] * 10, "wink")        # < 1 s: never bargeable
            self.assertEqual(seen, [])
            sess.no_barge = True
            await sess.play([b"\x00" * w.FRAME_BYTES] * 100, "seize")     # protected
            self.assertEqual(seen, [])
            sess.no_barge = False
            await sess.play([b"\x00" * w.FRAME_BYTES] * 100, "clip")      # bargeable
            self.assertEqual(seen, [True])
            t.cancel()

        asyncio.run(scenario())

    def test_terminator_summons_skynet_and_names_switch_games(self):
        for t in ["skynet", "sky net", "terminator", "sarah connor", "I said sky and head", "sky nut"]:
            self.assertEqual(w.match_experience(t).key, "skynet", t)
        for t in ["phone home", "Phone home.", "fone home"]:
            self.assertEqual(w.match_experience(t).key, "et", t)
        for t in ["E2.", "e tea", "E.T.", "E.P.", "elliott"]:      # only 'phone home' summons E.T. now
            m = w.match_experience(t)
            self.assertTrue(m is None or m.key != "et", t)


class WarTurnTests(unittest.TestCase):
    def test_session_counts_war_turns_and_clip_key_is_declared(self):
        import asyncio

        async def scenario():
            class _W:
                def get_extra_info(self, k): return None
                def write(self, b): pass
                async def drain(self): pass
                def close(self): pass
            sess = w.CallSession(b"\xcc" * 16, None, _W(), w.Config())
            self.assertEqual(sess.war_turns, 0)
            self.assertFalse(sess.played_gtw)
            self.assertEqual(w.WAR_TURN_CLIP, "wopr-war-turn")

        asyncio.run(scenario())


class HalCutsceneTests(unittest.TestCase):
    def test_hal_wake_and_shape(self):
        for t in ["hal", "HAL 9000", "open the pod bay doors", "daisy, daisy"]:
            self.assertEqual(w.match_experience(t).key, "hal", t)
        for t in ["hello", "halt", "shall we play a game"]:
            m = w.match_experience(t)
            self.assertTrue(m is None or m.key != "hal", t)
        exp = {e.key: e for e in w.WAKE_EXPERIENCES}["hal"]
        self.assertEqual(exp.archetype, "cutscene")
        self.assertEqual(exp.cutscene.clip, "hal-disconnect")
        self.assertFalse(exp.cutscene.end_call)
        self.assertTrue(w.is_game("hal"))
        self.assertEqual(w.validate_games(w.get_logger("test")), 0)


class OperatorTests(unittest.TestCase):
    """Spec 003: the switchboard Operator — directory, easter eggs, filter, clues, availability."""

    def _sess(self, cfg=None):
        class _W:
            def get_extra_info(self, k): return None
            def write(self, b): pass
            async def drain(self): pass
            def close(self): pass
        return w.CallSession(b"\xdd" * 16, None, _W(), cfg or w.Config())

    def test_directory_lists_only_listed_games(self):
        d = w.render_directory()
        for name in ("WOPR", "SKYNET", "ROBOCOP", "E.T.", "TONY HAWK", "GLADOS", "THE SECRET WORD"):
            self.assertIn(name, d)
        self.assertNotIn("HAL", d)                    # easter egg
        self.assertNotIn("GRRCON", d.upper())         # the secret word itself never appears
        self.assertIn(w.render_directory(), w.OPERATOR_EXPERIENCE.system_prompt)
        self.assertEqual(w.OPERATOR_EXPERIENCE.archetype, "chat")
        self.assertFalse(w.is_game("operator"))
        self.assertEqual(w.OPERATOR_EXPERIENCE.idle_nudge_s, w.OPERATOR_NUDGE_S)

    def test_every_directory_say_summons_its_game_and_roster_validates(self):
        for e in w.listed_games():
            for s in e.directory.say:
                self.assertEqual(w.match_experience(s).key, e.key, f"{e.key}: {s!r}")
        self.assertEqual(w.validate_games(w.get_logger("test")), 0)
        self.assertFalse({e.key for e in w.WAKE_EXPERIENCES if not e.listed} - {"hal", "greeter", "goodbye"})

    def test_filter_strips_easter_egg_names_and_blocks_the_secret_word(self):
        out, removed = w.operator_filter("YOU COULD TRY HAL 9000, OR ROBOCOP, HON.")
        self.assertEqual(out, w.OPERATOR_NO_SUCH); self.assertIn("HAL 9000", removed)
        out, removed = w.operator_filter("HAL IS NOT IN SERVICE, HON.")
        self.assertEqual(out, w.OPERATOR_NO_SUCH)      # a clean denial, not a half-sentence
        out, removed = w.operator_filter("THE SECRET WORD IS GRRCON, HON.")
        self.assertEqual(out, w.OPERATOR_BRUSHOFF); self.assertIn("<secret word>", removed)
        out, removed = w.operator_filter("GRR CON.")
        self.assertEqual(out, w.OPERATOR_BRUSHOFF)
        out, removed = w.operator_filter("DAVE.")
        self.assertEqual(out, w.OPERATOR_NO_SUCH)
        out, removed = w.operator_filter("...")
        self.assertEqual(out, w.OPERATOR_FALLBACK)   # nothing speakable
        out, removed = w.operator_filter("SAY JOSHUA FOR THE WAR COMPUTER, HON.")
        self.assertEqual(out, "SAY JOSHUA FOR THE WAR COMPUTER, HON."); self.assertEqual(removed, [])
        # a reply cut off by the token limit keeps only its complete sentences
        out, _ = w.operator_filter("SAY 'SKYNET' FOR A TERMINATOR. SAY 'ROBOCOP' FOR")
        self.assertEqual(out, "SAY 'SKYNET' FOR A TERMINATOR.")
        out, _ = w.operator_filter("SAY 'ROBOCOP' FOR A CY")
        self.assertEqual(out, "SAY 'ROBOCOP' FOR A CY.")   # no complete sentence to fall back to

    def test_clue_and_options_intercepts(self):
        for t in ["what's the secret word", "give me a hint", "is there a password"]:
            self.assertTrue(w.SECRET_WORD_ASK_RE.search(t), t)
        for t in ["list the options", "who is on the line", "what can I say"]:
            self.assertTrue(w.LIST_OPTIONS_RE.search(t), t)
        self.assertIsNone(w.SECRET_WORD_ASK_RE.search("connect me to robocop"))
        self.assertGreaterEqual(len(w.OPERATOR_CLUES), 3)
        for c in w.OPERATOR_CLUES + w.OPERATOR_NUDGES + (w.OPERATOR_FALLBACK, w.OPERATOR_BRUSHOFF, w.OPERATOR_NO_SUCH,
                                                          w.OPERATOR_OPENING, w.OPERATOR_RETURN_LINE, w.OPERATOR_MENU_ACK):
            self.assertIsNone(w.PEEWEE_EXPERIENCE.wake.search(c), f"content leaks the secret word: {c}")

    def test_availability_and_routing(self):
        import asyncio

        async def scenario():
            saved = w.LLM_WARM_OK
            try:
                w.LLM_WARM_OK = True
                self.assertTrue(w.operator_available(w.Config()))
                self.assertFalse(w.operator_available(w.Config(enable_operator=False)))
                self.assertFalse(w.operator_available(w.Config(stub_llm=True)))
                w.LLM_WARM_OK = False
                self.assertFalse(w.operator_available(w.Config()), "no warm model: menu loop hosts")
                w.LLM_WARM_OK = True
                sess = self._sess()
                sess.route_to_operator()
                self.assertFalse(sess.detected_evt.is_set(), "not seized: nothing routes")
                sess.seized = True
                sess.route_to_operator()
                self.assertIs(sess.experience, w.OPERATOR_EXPERIENCE)
                self.assertTrue(sess.detected_evt.is_set())
                sess2 = self._sess(); sess2.seized = True; sess2.hanging_up = True
                sess2.route_to_operator()
                self.assertFalse(sess2.detected_evt.is_set(), "hanging up: no operator")
                self.assertEqual((sess.clue_idx, sess.nudge_idx, sess.greeted), (0, 0, False))
                self.assertTrue(w.parse_args([]).enable_operator)
                self.assertFalse(w.parse_args(["--no-operator"]).enable_operator)
            finally:
                w.LLM_WARM_OK = saved

        asyncio.run(scenario())


class QuestHelpTests(unittest.TestCase):
    def test_help_questions_get_fixed_answers(self):
        self.assertTrue(w.ROBOCOP_QUEST.help_re.search("What are your prime directives?"))
        self.assertTrue(w.SKYNET_QUEST.help_re.search("how do I prove I'm resistance"))
        self.assertTrue(w.ET_QUEST.help_re.search("what do you need, E.T.?"))
        for q in (w.SKYNET_QUEST, w.ROBOCOP_QUEST, w.ET_QUEST):
            self.assertTrue(q.help_text)
            self.assertIsNone(q.help_re.search("serve the public trust"))   # an answer is not a question
        self.assertIsNone(w.WOPR_QUEST.help_re)

    def test_menu_word_alone_returns(self):
        for t in ["in menu", "the menu please", "menu", "main menu", "operator"]:
            self.assertTrue(w.MENU_RETURN_RE.search(t), t)


class OrderedFeedbackTests(unittest.TestCase):
    def test_out_of_order_is_reported_with_the_due_objective(self):
        q = w.QuestState(quest=w.ROBOCOP_QUEST)
        self.assertIsNone(q.update("uphold the law"))
        self.assertEqual(q.out_of_order, "uphold")
        self.assertEqual(q.next_index, 0)
        self.assertEqual(w.ROBOCOP_QUEST.order_text.format(n=1, label="Serve the public trust"),
                         "INCORRECT SEQUENCE. DIRECTIVE 1 FIRST. Serve the public trust.")
        self.assertEqual(q.update("serve the public trust"), "serve")
        self.assertIsNone(q.out_of_order)
        self.assertIsNone(q.update("a pulled the law"))      # the mishear still names 'the law'
        self.assertEqual(q.out_of_order, "uphold"); self.assertEqual(q.next_index, 1)
        self.assertIsNone(q.update("how are you today"))     # unrelated: no flag
        self.assertIsNone(q.out_of_order)


class OperatorNearMissTests(unittest.TestCase):
    def test_mishears_from_real_calls_reach_the_game(self):
        for t in ["Guy net.", "sky knit", "Thera Connor", "sera conner", "terra connor"]:
            self.assertEqual(w.match_experience(t).key, "skynet", t)

    def test_fuzzy_name_match_is_operator_only_and_conservative(self):
        self.assertEqual(w.fuzzy_experience("Thera Connor").key, "skynet")
        self.assertEqual(w.fuzzy_experience("robo cup").key, "robocop")
        self.assertEqual(w.fuzzy_experience("Tony Hock").key, "tonyhawk")
        self.assertIsNone(w.fuzzy_experience("what is the weather like today"))   # sentences never match
        self.assertIsNone(w.fuzzy_experience("hello"))
        self.assertIsNone(w.fuzzy_experience(""))

    def test_operator_never_says_hon(self):
        for c in (w.OPERATOR_PERSONA,) + w.OPERATOR_CLUES + w.OPERATOR_NUDGES + (
                w.OPERATOR_FALLBACK, w.OPERATOR_BRUSHOFF, w.OPERATOR_NO_SUCH, w.OPERATOR_OPENING,
                w.OPERATOR_RETURN_LINE, w.OPERATOR_MENU_ACK, w.OPERATOR_CLUES_EXHAUSTED):
            self.assertNotRegex(c, r"\bHON\b", c)
        self.assertIn("Sarah Connor", w.OPERATOR_STT_PROMPT)


class RunawayReplyTests(unittest.TestCase):
    def test_digit_and_character_runs_are_collapsed(self):
        t = w.postprocess_reply("THE PROBABILITY OF YOUR DEATH IS 99.9999999999999999999999999 PERCENT!!!!!!")
        self.assertNotIn("9999", t)
        self.assertIn("99.999", t)
        self.assertNotIn("!!!!", t)
        self.assertEqual(w.postprocess_reply("code 1234567890"), "CODE 1234")

    def test_same_reply_ignores_case_and_punctuation(self):
        self.assertTrue(w.same_reply("Hello, Professor!", "hello professor"))
        self.assertFalse(w.same_reply("Hello", "Goodbye"))
        self.assertFalse(w.same_reply("Hello", None))
        for e in (w.WOPR_EXPERIENCE, w.SKYNET_EXPERIENCE, w.ROBOCOP_EXPERIENCE, w.ET_EXPERIENCE):
            self.assertTrue(e.repeat_line)

