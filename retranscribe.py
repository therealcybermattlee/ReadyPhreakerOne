#!/usr/bin/env python3
"""
retranscribe.py — offline high-quality transcription of the recorded calls.

Live transcription uses base.en for speed; this re-runs a bigger Whisper model on the recorded
*caller-only* legs (the `-caller.wav` files the dialplan writes) with no latency pressure, to
produce a clean dataset for the word cloud and the study.

  python retranscribe.py [--recdir /opt/wopr/recordings] [--out /opt/wopr/transcripts/retranscript.jsonl]
                         [--model medium.en] [--mixed]

One JSONL record per recording: {file, ts, seconds, text}. `--mixed` transcribes the mixed
call file instead of the caller leg (includes WOPR's speech too). Needs faster-whisper installed;
the model downloads on first use (run once with internet).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import wave


def wav_seconds(path: str) -> float:
    try:
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:  # noqa: BLE001
        return 0.0


def timestamp_from_name(path: str) -> str:
    m = re.search(r"(\d{8})-(\d{6})", os.path.basename(path))
    if not m:
        return ""
    d, t = m.group(1), m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recdir", default="/opt/wopr/recordings")
    p.add_argument("--out", default="/opt/wopr/transcripts/retranscript.jsonl")
    p.add_argument("--model", default="medium.en")
    p.add_argument("--mixed", action="store_true", help="transcribe the mixed call file, not the caller leg")
    p.add_argument("--prompt", default=("WOPR from WarGames. Joshua, Professor Falken, shall we play a "
                                        "game, chess, checkers, global thermonuclear war, the only "
                                        "winning move is not to play."))
    a = p.parse_args(argv)

    pattern = "*.wav" if a.mixed else "*-caller.wav"
    files = sorted(f for f in glob.glob(os.path.join(a.recdir, pattern))
                   if a.mixed or f.endswith("-caller.wav"))
    if a.mixed:
        files = [f for f in sorted(glob.glob(os.path.join(a.recdir, "*.wav"))) if not f.endswith("-caller.wav")]
    if not files:
        print(f"No recordings matching in {a.recdir}", file=sys.stderr)
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed in this environment", file=sys.stderr)
        return 2

    print(f"Loading {a.model} (downloads on first use)...", file=sys.stderr)
    model = WhisperModel(a.model, device="cpu", compute_type="int8")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    n = 0
    with open(a.out, "w", encoding="utf-8") as out:
        for f in files:
            segs, _info = model.transcribe(f, language="en", beam_size=5,
                                           condition_on_previous_text=False, initial_prompt=a.prompt)
            text = " ".join(s.text.strip() for s in segs).strip()
            rec = {"file": os.path.basename(f), "ts": timestamp_from_name(f),
                   "seconds": round(wav_seconds(f), 1), "text": text}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            print(f"  {os.path.basename(f):40} {text[:70]}", file=sys.stderr)
    print(f"\n{n} recordings -> {a.out}", file=sys.stderr)
    print("Point the word cloud at it:  analyze.py --transcript "
          f"{a.out} --user-field text --html report.html", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
