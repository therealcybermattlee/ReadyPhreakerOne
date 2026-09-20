#!/usr/bin/env python3
"""
analyze.py — the "human experiment" data study for ReadyPhreakerOne.

Reads the per-call metrics (sessions.jsonl, written by the daemon) and the turn transcripts
(dataset.jsonl) and produces:
  - a text report to stdout (funnel, point distribution, timings, win rate, callbacks)
  - an HTML report with a word cloud of what callers said to WOPR (--html OUT)

Stdlib only.

  python analyze.py [--sessions /opt/wopr/transcripts/sessions.jsonl]
                    [--transcript /opt/wopr/transcripts/dataset.jsonl]
                    [--html report.html] [--top 120]
"""
from __future__ import annotations

import argparse
import html
import json
import math
import random
import re
import statistics
import sys
from collections import Counter

BEATS = ["joshua", "play_game", "global_thermonuclear_war", "winning_move"]
BEAT_LABEL = {"joshua": "Said “Joshua”", "play_game": "Asked to play a game",
              "global_thermonuclear_war": "Chose thermonuclear war",
              "winning_move": "“The only winning move…”"}

# The playable games (matches the daemon's roster) and their display names.
GAME_LABEL = {"wopr": "WOPR (WarGames)", "skynet": "Skynet / T-800", "robocop": "RoboCop",
              "et": "E.T.", "tonyhawk": "Tony Hawk", "glados": "GLaDOS (song)",
              "peewee": "Pee-wee (Tequila)", "hal": "HAL 9000"}

STOPWORDS = set(("a an and the to of is it i you we they he she that this these those for on in at "
                 "with as be are was were do does did have has had will would can could should i'm "
                 "im its it's my your our their me him her us them what who how why when where which "
                 "not no yes ok okay so if then than but or nor about just like get got go going want "
                 "know think really very there here up down out over all any some more most one two "
                 "hello hi hey um uh oh yeah yep nope gonna wanna let lets let's").split())


def load_jsonl(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return rows


def stat_line(name, values, unit="ms"):
    vals = [v for v in values if v is not None]
    if not vals:
        return f"  {name:32} (no data)"
    if unit == "s":
        vals = [v / 1000 for v in vals]
    fmt = (lambda x: f"{x:.1f}") if unit == "s" else (lambda x: f"{x:.0f}")
    return (f"  {name:32} n={len(vals):<4} median={fmt(statistics.median(vals))}{unit}  "
            f"mean={fmt(statistics.mean(vals))}{unit}  min={fmt(min(vals))}{unit}  max={fmt(max(vals))}{unit}")


def build_report(sessions: list[dict], turns: list[dict]) -> tuple[str, dict]:
    # A "player" is one inbound call (someone lifting the payphone). Callbacks are WOPR-initiated
    # follow-ups, not new players, so the study below is computed over inbound only.
    inbound = [s for s in sessions if not s.get("callback")]
    callbacks = [s for s in sessions if s.get("callback")]
    whistled = [s for s in inbound if s.get("whistled")]
    won = [s for s in inbound if s.get("won")]
    L = []
    L.append("=" * 66)
    L.append("  WOPR — HUMAN EXPERIMENT REPORT")
    L.append("=" * 66)
    L.append(f"  Players (payphone lifted):  {len(inbound)}")
    L.append(f"  WOPR callbacks (not players):{len(callbacks)}  "
             f"(sim={sum(1 for s in callbacks if s['callback']=='sim')}, "
             f"win={sum(1 for s in callbacks if s['callback']=='win')})")
    if inbound:
        L.append(f"  Whistled to WOPR:           {len(whistled)}/{len(inbound)} "
                 f"({100*len(whistled)/len(inbound):.0f}%)")
    L.append(f"  Won the WOPR flag:          {len(won)}/{len(inbound)}  "
             f"({100*len(won)/max(len(inbound),1):.0f}% of players)   "
             f"(WOPR only; every game's wins/completions are in the per-game table)")
    L.append("")

    # --- per-game breakdown: which characters players chose -------------------------------------
    pdenom = len(inbound) or 1
    game_stats = {}   # key -> counters
    for s in inbound:
        for key, g in (s.get("games") or {}).items():
            if key not in GAME_LABEL:
                continue
            gs = game_stats.setdefault(key, {"players": 0, "turns": 0, "wins": 0, "completions": 0,
                                             "clips": 0, "chatted": 0})
            if g.get("reached") or g.get("clip"):
                gs["players"] += 1
            gs["turns"] += g.get("turns", 0)
            if g.get("turns", 0) > 0:
                gs["chatted"] += 1
            gs["wins"] += 1 if g.get("won") else 0                  # quest games: objectives met
            gs["completions"] += 1 if g.get("completed") else 0     # cutscenes: played to the end
            gs["clips"] += 1 if g.get("clip") else 0
    primary = Counter(s.get("primary_game") for s in inbound if s.get("primary_game"))
    L.append("  PER-GAME BREAKDOWN (which characters players chose)")
    if game_stats:
        L.append(f"    {'game':<17}{'players':>8}{'%':>5}{'chatted':>9}{'turns':>7}{'avg':>6}{'wins':>6}"
                 f"{'compl':>7}{'clips':>6}")
        order = sorted(game_stats, key=lambda k: game_stats[k]["players"], reverse=True)
        for key in order:
            gs = game_stats[key]
            avg = gs["turns"] / gs["chatted"] if gs["chatted"] else 0.0
            L.append(f"    {GAME_LABEL[key]:<17}{gs['players']:>8}{100*gs['players']/pdenom:>4.0f}%"
                     f"{gs['chatted']:>9}{gs['turns']:>7}{avg:>6.1f}{gs['wins']:>6}{gs['completions']:>7}"
                     f"{gs['clips']:>6}")
        mx = max(gs["players"] for gs in game_stats.values()) or 1
        L.append("")
        L.append("    popularity (players who reached each game)")
        for key in order:
            n = game_stats[key]["players"]
            L.append(f"    {GAME_LABEL[key]:<17}{n:>4}  {'#' * int(round(28 * n / mx))}")
        if primary:
            L.append("")
            L.append("    most-engaged game per player (by conversation turns)")
            for key, n in primary.most_common():
                lbl = GAME_LABEL.get(key, key)
                L.append(f"    {lbl:<17}{n:>4}  {'#' * int(round(28 * n / (max(primary.values()) or 1)))}")
    else:
        L.append("    (no game data yet — records predate per-game tracking, or no games played)")
    L.append("")

    L.append("  ── the sections below are WOPR-game-specific (only WOPR players reach them) ──")
    L.append("")
    L.append("  FUNNEL (how far WOPR players got)")
    denom = len(inbound) or 1
    L.append(f"    lifted & whistled     {len(whistled):>4}  {100*len(whistled)/denom:5.0f}%")
    for b in BEATS:
        c = sum(1 for s in inbound if b in (s.get("beats") or []))
        L.append(f"    {BEAT_LABEL[b]:<22}{c:>4}  {100*c/denom:5.0f}%")
    L.append(f"    WON (all beats)       {len(won):>4}  {100*len(won)/denom:5.0f}%")
    L.append("")
    L.append("  POINTS PER PLAYER (quest beats, 0–4)")
    pts = Counter(s.get("points", 0) for s in inbound)
    for p in range(5):
        bar = "#" * pts.get(p, 0)
        L.append(f"    {p} pts  {pts.get(p,0):>4}  {bar}")
    L.append("")
    L.append("  TIMINGS (players)")
    L.append(stat_line("Music heard before whistle", [s.get("attract_ms") for s in inbound], "s"))
    L.append(stat_line("Whistle → first point", [s.get("first_point_after_whistle_ms") for s in inbound], "s"))
    L.append(stat_line("Call start → win", [s.get("win_ms") for s in won], "s"))
    L.append(stat_line("Call duration", [s.get("duration_ms") for s in inbound], "s"))
    L.append(stat_line("Turns per call", [s.get("turns") for s in inbound], ""))
    L.append("")
    L.append("  WHISTLE ACCURACY (measured pitch of the human whistle, target 2600 Hz)")
    hzs = [s.get("whistle_hz") for s in inbound if s.get("whistle_hz") is not None]
    if hzs:
        errs = [h - 2600 for h in hzs]
        L.append(f"    n={len(hzs)}   median {statistics.median(hzs):.0f} Hz   mean {statistics.mean(hzs):.0f} Hz"
                 f"   range {min(hzs):.0f}–{max(hzs):.0f} Hz")
        L.append(f"    mean error {statistics.mean(errs):+.0f} Hz   "
                 f"mean |error| {statistics.mean(abs(e) for e in errs):.0f} Hz   "
                 f"within ±25 Hz: {100*sum(1 for e in errs if abs(e)<=25)/len(errs):.0f}%")
        buckets = Counter(int(round((h - 2600) / 50.0)) * 50 for h in hzs)
        for b in sorted(buckets):
            L.append(f"    {2600 + b:>4} Hz ({b:+4d})  {buckets[b]:>3}  {'#' * buckets[b]}")
    else:
        L.append("    (no whistles measured yet)")
    L.append("")
    L.append("  LEADERBOARD (top players by composite score)")
    ranked = sorted(inbound, key=score_call, reverse=True)[:10]
    L.append(f"    {'#':>2}  {'call':8}  {'score':>5}  {'pts':>3}  {'won':>3}  {'win_s':>5}  {'whistle':>8}")
    for i, s in enumerate(ranked, 1):
        wm = s.get("win_ms"); hz = s.get("whistle_hz")
        L.append(f"    {i:>2}  {s.get('call','?'):8}  {score_call(s):>5}  {s.get('points',0):>3}  "
                 f"{'Y' if s.get('won') else '-':>3}  {(f'{wm/1000:.0f}' if wm else '-'):>5}  "
                 f"{(f'{hz:.0f}Hz' if hz else '-'):>8}")
    L.append("=" * 66)
    summary = {"players": len(inbound), "callbacks": len(callbacks),
               "whistled": len(whistled), "won": len(won)}
    return "\n".join(L), summary


def score_call(s: dict) -> int:
    """Composite 'player' score for a single call:
      points   : 250 each (0–1000)
      win      : +1000, plus a speed bonus up to +1000 (faster win = more)
      whistle  : accuracy bonus up to +200 (dead-on 2600 Hz = full)
    """
    pts = s.get("points", 0) * 250
    win = 0
    if s.get("won"):
        win = 1000
        wm = s.get("win_ms")
        if wm is not None:
            win += max(0, round(1000 - wm / 60.0))       # ~60 s to zero the speed bonus
    acc = 0
    err = s.get("whistle_hz_err")
    if err is not None:
        acc = max(0, round(200 - abs(err) * 2))          # within ~100 Hz earns something
    return pts + win + acc


def word_freqs(turns: list[dict], top: int, field: str = "user") -> list[tuple[str, int]]:
    """Frequencies of words callers said (the `field` side of each record)."""
    c = Counter()
    for t in turns:
        text = (t.get(field) or "").lower()
        for w in re.findall(r"[a-z']+", text):
            w = w.strip("'")
            if len(w) >= 3 and w not in STOPWORDS:
                c[w] += 1
    return c.most_common(top)


def render_html(report_text: str, freqs: list[tuple[str, int]], summary: dict) -> str:
    if freqs:
        hi = freqs[0][1]
        lo = freqs[-1][1]
        span = max(hi - lo, 1)
        palette = ["#39ff14", "#00e5ff", "#ffd500", "#ff6ec7", "#7cfc00", "#ff8c00"]
        words = list(freqs)
        random.Random(2600).shuffle(words)
        chunks = []
        for w, n in words:
            size = 14 + round(60 * (n - lo) / span)
            color = palette[(size) % len(palette)]
            chunks.append(f'<span style="font-size:{size}px;color:{color}" title="{n}">'
                          f'{html.escape(w)}</span>')
        cloud = " ".join(chunks)
    else:
        cloud = '<em>no caller speech yet</em>'
    cards = "".join(
        f'<div class="card"><div class="num">{v}</div><div class="lbl">{k}</div></div>'
        for k, v in [("players", summary["players"]), ("whistled", summary["whistled"]),
                     ("won flag", summary["won"]), ("callbacks", summary["callbacks"])])
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>WOPR — Human Experiment Report</title>
<style>
  body{{background:#000;color:#39ff14;font-family:'Courier New',monospace;margin:0;padding:28px}}
  h1{{letter-spacing:3px;font-weight:400;border-bottom:1px solid #145214;padding-bottom:10px}}
  .cards{{display:flex;gap:16px;flex-wrap:wrap;margin:18px 0}}
  .card{{background:#061206;border:1px solid #145214;border-radius:8px;padding:14px 20px;min-width:120px}}
  .num{{font-size:34px;color:#7cfc00}} .lbl{{opacity:.7;text-transform:uppercase;font-size:12px}}
  pre{{background:#050b05;border:1px solid #145214;border-radius:8px;padding:18px;overflow:auto}}
  .cloud{{background:#050b05;border:1px solid #145214;border-radius:8px;padding:26px;line-height:2.1;text-align:center}}
  .cloud span{{margin:0 8px;white-space:nowrap;text-shadow:0 0 6px currentColor}}
  h2{{color:#00e5ff;font-weight:400;letter-spacing:2px}}
  .foot{{opacity:.5;font-size:12px;margin-top:24px}}
</style></head><body>
<h1>WOPR // HUMAN EXPERIMENT REPORT</h1>
<div class="cards">{cards}</div>
<h2>SHALL WE PLAY A GAME? — WHAT THEY SAID</h2>
<div class="cloud">{cloud}</div>
<h2>THE NUMBERS</h2>
<pre>{html.escape(report_text)}</pre>
<div class="foot">A STRANGE GAME. THE ONLY WINNING MOVE IS NOT TO PLAY.</div>
</body></html>"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sessions", default="/opt/wopr/transcripts/sessions.jsonl")
    p.add_argument("--transcript", default="/opt/wopr/transcripts/dataset.jsonl")
    p.add_argument("--html", default="")
    p.add_argument("--top", type=int, default=120)
    p.add_argument("--user-field", default="user", help="transcript field holding caller text ('user', or 'text' for retranscript.jsonl)")
    a = p.parse_args(argv)

    sessions = load_jsonl(a.sessions)
    turns = load_jsonl(a.transcript)
    if not sessions and not turns:
        print(f"No data found at {a.sessions} or {a.transcript}", file=sys.stderr)
        return 1

    report, summary = build_report(sessions, turns)
    print(report)
    freqs = word_freqs(turns, a.top, a.user_field)
    if freqs:
        print("\n  TOP WORDS CALLERS SAID")
        for w, n in freqs[:20]:
            print(f"    {w:<18} {n}")
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(render_html(report, freqs, summary))
        print(f"\nHTML report → {a.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
