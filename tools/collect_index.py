#!/usr/bin/env python3
"""Build a human-usable index of a ReadyPhreakerOne run from the mini's data.

Inputs (defaults assume the layout `collect.sh` creates on the external drive):
  <root>/transcripts/sessions.jsonl   one record per call (metrics)
  <root>/transcripts/dataset.jsonl    one record per turn (what was said, both sides)
  <root>/logs/wopr.log                the daemon log (every whistle, switch, flag, barge-in)
  <root>/recordings/*.wav             <YYYYmmdd-HHMMSS local>-<asterisk epoch.seq>[-caller].wav

Outputs in <root>: CALLS.csv, CALLS.md (per day), DAILY.md, README.md.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, json, os, re, sys, zoneinfo
from collections import defaultdict, Counter

LOCAL = zoneinfo.ZoneInfo("America/Detroit")
LOG_RE = re.compile(r"^(\S+) (INFO|WARNING|ERROR) call=([0-9a-f]+|-) (.*)$")
KV_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')
REC_RE = re.compile(r"^(\d{8}-\d{6})-(\d+)\.(\d+)(-caller)?\.wav$")
GAME_LABEL = {"wopr": "WOPR", "skynet": "Skynet", "robocop": "RoboCop", "et": "E.T.", "tonyhawk": "Tony Hawk",
              "glados": "GLaDOS", "peewee": "Pee-wee", "hal": "HAL", "ghostbusters": "Ghostbusters", "operator": "Operator", "greeter": "hints",
              "goodbye": "goodbye"}


def parse_kv(rest: str) -> dict:
    return {k: v.strip('"') for k, v in KV_RE.findall(rest)}


def utc(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def load_log(path: str) -> dict:
    """Per call: start, path of experiences, flags, master flag, barge-ins, whistle, rejected STT."""
    calls: dict[str, dict] = defaultdict(lambda: {"path": [], "flags": [], "master": False, "bargeins": 0,
                                                   "whistle_hz": None, "stt_rejected": 0, "menu_returns": 0,
                                                   "start": None, "end_reason": None, "start_epoch": None})
    if not os.path.exists(path):
        return calls
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_RE.match(line.rstrip("\n"))
            if not m or m.group(3) == "-":
                continue
            ts, _lvl, call, rest = m.groups()
            kv = parse_kv(rest)
            ev = kv.get("event")
            c = calls[call]
            if ev == "call_start":
                c["start"] = utc(ts); c["start_epoch"] = int(c["start"].timestamp())
            elif ev == "experience":
                k = kv.get("key")
                if k and (not c["path"] or c["path"][-1] != k):
                    c["path"].append(k)
            elif ev == "flag_unlocked":
                c["flags"].append(kv.get("key") or "wopr")
            elif ev == "master_flag":
                c["master"] = True
            elif ev == "bargein_speech":
                c["bargeins"] += 1
            elif ev == "detect" and c["whistle_hz"] is None:
                try: c["whistle_hz"] = round(float(kv.get("hz", "nan")))
                except ValueError: pass
            elif ev == "stt_rejected":
                c["stt_rejected"] += 1
            elif ev == "menu_return":
                c["menu_returns"] += 1
            elif ev == "call_end":
                c["end_reason"] = kv.get("reason")
    return calls


def load_sessions(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[r.get("call")] = r
    return out


def load_turns(path: str) -> dict:
    turns = defaultdict(list)
    if not os.path.exists(path):
        return turns
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        turns[r.get("call")].append(r)
    return turns


def load_recordings(path: str) -> dict:
    """epoch -> {'mixed': name, 'caller': name}"""
    recs = defaultdict(dict)
    if not os.path.isdir(path):
        return recs
    for name in os.listdir(path):
        m = REC_RE.match(name)
        if not m:
            continue
        epoch = int(m.group(2))
        recs[epoch]["caller" if m.group(4) else "mixed"] = name
    return recs


def match_recording(recs: dict, epoch: int | None) -> dict:
    if epoch is None:
        return {}
    for d in (0, -1, 1, -2, 2, -3, 3):
        if epoch + d in recs:
            return recs[epoch + d]
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--since", help="keep only calls on or after this local date (YYYY-MM-DD), e.g. the event day")
    a = ap.parse_args()
    root = a.root
    log = load_log(os.path.join(root, "logs", "wopr.log"))
    sessions = load_sessions(os.path.join(root, "transcripts", "sessions.jsonl"))
    turns = load_turns(os.path.join(root, "transcripts", "dataset.jsonl"))
    recs = load_recordings(os.path.join(root, "recordings"))

    rows = []
    for call, s in sessions.items():
        lg = log.get(call, {})
        start = lg.get("start") or utc(s.get("ts", "1970-01-01T00:00:00Z"))
        if lg.get("start") is None:   # sessions.ts is the END time; approximate the start
            start = start - dt.timedelta(milliseconds=s.get("duration_ms") or 0)
        local = start.astimezone(LOCAL)
        rec = match_recording(recs, lg.get("start_epoch") or int(start.timestamp()))
        games = [k for k in lg.get("path", []) if k not in ("operator", "greeter", "goodbye")]
        flags = lg.get("flags") or [k for k, g in (s.get("games") or {}).items() if g.get("won")]
        ops = sum(1 for t in turns.get(call, []) if t.get("kind") == "operator")
        rows.append({
            "date": local.strftime("%Y-%m-%d"), "start_local": local.strftime("%H:%M:%S"),
            "call": call, "duration_s": round((s.get("duration_ms") or 0) / 1000),
            "whistled": "yes" if s.get("whistled") else "no",
            "whistle_hz": lg.get("whistle_hz") or "",
            "games": " > ".join(GAME_LABEL.get(k, k) for k in games),
            "flags": " ".join(GAME_LABEL.get(k, k) for k in flags), "n_flags": len(flags),
            "master_flag": "YES" if lg.get("master") else "",
            "turns": s.get("turns", 0), "operator_turns": ops,
            "bargeins": lg.get("bargeins", 0), "menu_returns": lg.get("menu_returns", 0),
            "stt_rejected": lg.get("stt_rejected", 0),
            "end": s.get("reason") or lg.get("end_reason") or "",
            "recording": rec.get("mixed", ""), "caller_leg": rec.get("caller", ""),
            "_sort": start,
        })
    if a.since:
        rows = [r for r in rows if r["date"] >= a.since]
    rows.sort(key=lambda r: r["_sort"])

    fields = [k for k in rows[0].keys() if not k.startswith("_")] if rows else []
    with open(os.path.join(root, "CALLS.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    with open(os.path.join(root, "CALLS.md"), "w", encoding="utf-8") as f:
        f.write("# ReadyPhreakerOne — every call\n\n")
        f.write("One row per call. Times are local (America/Detroit). `games` is the path the caller took; "
                "`flags` are the quests won; recordings are in `recordings/` (mixed = both sides, caller_leg = the handset only).\n\n")
        for day in sorted(by_day):
            rs = by_day[day]
            nflags = sum(r["n_flags"] for r in rs)
            f.write(f"## {day} — {len(rs)} calls, {sum(1 for r in rs if r['whistled']=='yes')} whistled, "
                    f"{nflags} flags, {sum(1 for r in rs if r['master_flag'])} master flags\n\n")
            f.write("| start | dur | whistle | path | flags | 5th | turns | op | recording |\n|---|---|---|---|---|---|---|---|---|\n")
            for r in rs:
                dur = f"{r['duration_s']//60}:{r['duration_s']%60:02d}"
                f.write(f"| {r['start_local']} | {dur} | {r['whistle_hz'] or ('yes' if r['whistled']=='yes' else '-')} | "
                        f"{r['games'] or '-'} | {r['flags'] or '-'} | {r['master_flag'] or ''} | {r['turns']} | {r['operator_turns']} | "
                        f"`{r['recording'] or '-'}` |\n")
            f.write("\n")

    with open(os.path.join(root, "DAILY.md"), "w", encoding="utf-8") as f:
        f.write("# Daily summary\n\n| day | calls | whistled | reached a game | flags | master flags | longest | total talk time |\n|---|---|---|---|---|---|---|---|\n")
        for day in sorted(by_day):
            rs = by_day[day]
            longest = max(rs, key=lambda r: r["duration_s"])
            tot = sum(r["duration_s"] for r in rs)
            f.write(f"| {day} | {len(rs)} | {sum(1 for r in rs if r['whistled']=='yes')} | {sum(1 for r in rs if r['games'])} | "
                    f"{sum(r['n_flags'] for r in rs)} | {sum(1 for r in rs if r['master_flag'])} | "
                    f"{longest['duration_s']//60}:{longest['duration_s']%60:02d} ({longest['call']}) | {tot//3600}h{(tot%3600)//60:02d}m |\n")
        f.write("\n## Flags by game\n\n")
        cnt = Counter(k for r in rows for k in r["flags"].split() if k)
        for k, n in cnt.most_common():
            f.write(f"- {k}: {n}\n")
        f.write("\n## Games reached (any depth)\n\n")
        reach = Counter(g for r in rows for g in r["games"].split(" > ") if g)
        for k, n in reach.most_common():
            f.write(f"- {k}: {n}\n")

    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write(f"""# ReadyPhreakerOne — recordings & data

Collected from the Mac mini (`/opt/wopr`) on {dt.date.today().isoformat()}. {len(rows)} calls, {len(recs)} recordings.

## Layout

- `recordings/` — one pair of WAVs per call, 8 kHz mono: `<local time>-<asterisk epoch>.wav` is both sides mixed,
  `…-caller.wav` is the handset alone (what the caller said, plus the earpiece bleeding into the mouthpiece).
- `transcripts/sessions.jsonl` — one JSON record per call: duration, whistle pitch, per-game engagement
  (`games.<key>.{{reached,turns,won,completed}}`), the WOPR funnel fields (`points`, `beats`, `won`).
- `transcripts/dataset.jsonl` — one record per spoken exchange: what the caller said (`user`), what was said back
  (`wopr`), and `kind` (reply / objective / help / order / clue / operator / flag …).
- `transcripts/game_stats.json` — running aggregate per game (sessions, turns, wins, completions).
- `logs/wopr.log` — the daemon log, one `event=` per line, `call=<8 hex>` ties lines to a call. Every whistle
  (`detect`), seize, switch, barge-in, objective hit (`quest`), `flag_unlocked`, `master_flag`, rejected transcript.
- `logs/asterisk-launchd.log`, `logs/ollama.log`, `logs/pf.log` — the other services.

## Indexes (generated by `tools/collect_index.py`)

- `CALLS.csv` / `CALLS.md` — one row per call, in order, with the path taken, flags won, and the recording file names.
- `DAILY.md` — per-day totals, flags by game, games reached.
- `REPORT.txt` — the `analyze.py` human-experiment report over the whole run.

## Joining the pieces

The `call` id (8 hex) is the key between `sessions.jsonl`, `dataset.jsonl` and `wopr.log`. Recordings carry no call
id; they are matched by time: the Asterisk epoch in the file name equals the `call_start` time in the log to the second.
`CALLS.csv` has that join done.
""")
    print(f"{len(rows)} calls indexed; {sum(1 for r in rows if r['recording'])} matched to a recording; "
          f"{sum(r['n_flags'] for r in rows)} flags; {sum(1 for r in rows if r['master_flag'])} master flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
