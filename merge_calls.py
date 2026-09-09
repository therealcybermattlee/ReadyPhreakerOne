#!/usr/bin/env python3
"""
merge_calls.py — one WAV per player experience.

A single interaction is split across Asterisk calls: the inbound call, then any callback WOPR
placed (sim / win). This groups those together (a callback is attached to the inbound call it
followed, within --window seconds) and concatenates each group, in order, into one file per
experience. Uses the full-mix recordings (both sides), not the caller-only legs.

  python merge_calls.py [--recdir /opt/wopr/recordings] [--outdir /opt/wopr/merged]
                        [--window 240] [--gap-ms 700]

Output: <outdir>/<first-timestamp>-<uniqueid>-session.wav, one per experience. Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import wave

NAME_RE = re.compile(r"^(\d{8}-\d{6})-(\d{10})\.(\d+)(-cb|-win)?\.wav$")


def parse(path: str):
    """Return (timestamp, epoch, seq, suffix) for a full-mix recording, or None for others."""
    b = os.path.basename(path)
    if b.endswith("-caller.wav"):
        return None
    m = NAME_RE.match(b)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), (m.group(4) or "")[1:]


def read_wav(path):
    with wave.open(path, "rb") as w:
        return w.getparams(), w.readframes(w.getnframes())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recdir", default="/opt/wopr/recordings")
    p.add_argument("--outdir", default="/opt/wopr/merged")
    p.add_argument("--window", type=int, default=240, help="max seconds after an inbound call for a callback to belong to it")
    p.add_argument("--gap-ms", type=int, default=700, help="silence inserted between fragments")
    a = p.parse_args(argv)

    recs = []
    for f in glob.glob(os.path.join(a.recdir, "*.wav")):
        info = parse(f)
        if info:
            recs.append((info[1], info[2], info[3], info[0], f))   # epoch, seq, suffix, ts, path
    if not recs:
        print(f"No full-mix recordings in {a.recdir}", file=sys.stderr)
        return 1
    recs.sort()

    # group: an inbound (suffix "") opens a group; a callback joins the last group if close in time
    groups: list[list] = []
    for epoch, seq, suffix, ts, path in recs:
        if suffix == "" or not groups or (epoch - groups[-1][0][0]) > a.window:
            groups.append([(epoch, seq, suffix, ts, path)])
        else:
            groups[-1].append((epoch, seq, suffix, ts, path))

    os.makedirs(a.outdir, exist_ok=True)
    made = 0
    for g in groups:
        first = g[0]
        out = os.path.join(a.outdir, f"{first[3]}-{first[0]}.{first[1]}-session.wav")
        params = None
        gap_frames = b""
        with wave.open(out, "wb") as o:
            for i, (_e, _s, suffix, _ts, path) in enumerate(g):
                params_i, frames = read_wav(path)
                if params is None:
                    params = params_i
                    o.setnchannels(params.nchannels)
                    o.setsampwidth(params.sampwidth)
                    o.setframerate(params.framerate)
                    gap_frames = b"\x00" * (params.sampwidth * params.nchannels
                                            * int(params.framerate * a.gap_ms / 1000))
                if (params_i.nchannels, params_i.sampwidth, params_i.framerate) != \
                   (params.nchannels, params.sampwidth, params.framerate):
                    print(f"  skip (format mismatch): {os.path.basename(path)}", file=sys.stderr)
                    continue
                if i > 0:
                    o.writeframes(gap_frames)
                o.writeframes(frames)
        parts = "+".join(s or "call" for (_e, _s, s, _t, _p) in g)
        print(f"  {os.path.basename(out):42} ({len(g)} fragment{'s' if len(g) > 1 else ''}: {parts})")
        made += 1
    print(f"\n{made} session file(s) -> {a.outdir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
