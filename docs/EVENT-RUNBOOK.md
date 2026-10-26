# Event runbook (Hackoween edition)

The phone needs no internet. Ollama, Whisper and the clips all live on the mini. Wi-Fi only matters for you to reach it.

## Before leaving the house
- `ssh wopr-mini 'grep -E "event=(venue|games|listen)" /opt/wopr/log/wopr.log | tail -3'` → `venue=Hackoween`, `n=9`, `listen`.
- Make sure the mini can join a network you will have on site (venue Wi-Fi or your phone's hotspot).
  Add the hotspot as a known network on the mini **now**, at home, while it has a screen or while you can
  reach it: System Settings → Wi-Fi → Known Networks. On site, `ssh mattlee@<ip from the hotspot's client list>`.
- Pack: the payphone, the HT802 ATA, both power bricks, the Ethernet cable ATA↔mini (en0, the island),
  the printed tree (`docs/PHONE-TREE-PRINT.md`), the sound check (`docs/SOUND-CHECK.md`), a laptop.

## Power-up order on site
1. Mini first. Wait two minutes (Asterisk + the daemon + the pf firewall + the island DHCP all come up at boot).
2. ATA second, on the mini's en0 cable. It gets 192.168.50.2 from the mini and registers as `payphone`.
3. Pick up the handset → dial tone. Whistle → kerchunk → Matt's greeting. Done.

## If something is wrong
| Symptom | Check | Fix |
|---|---|---|
| no dial tone at all | `ssh wopr-mini 'sudo -n /opt/asterisk/sbin/asterisk -rx "pjsip show contacts"'` → `payphone … Avail` | power-cycle the ATA; wait 60 s; if still `Unavail`, reseat the Ethernet, then reboot the mini (`sudo -n /sbin/reboot`) |
| dial tone, whistle does nothing | `grep event=detect /opt/wopr/log/wopr.log \| tail` | whistle longer and closer; the detector wants ~0.6 s of steady tone. If no `detect` lines ever appear, `sudo -n /bin/launchctl kickstart -k system/com.crunchwhistle.wopr` |
| kerchunk, then silence | `grep -E "event=(menu_audio|play_start)" … \| tail` | restart the daemon (above). Clips load only at boot. |
| the Operator is deaf or slow | `curl -s localhost:11434/api/tags` on the mini; `pgrep -fl ollama` | `launchctl kickstart` the daemon; if Ollama is gone, reboot |
| can't ssh | the mini is on a different network than the laptop | join the same hotspot; the phone works regardless |
| the ATA has 169.254.x | pf dropped DHCP | `sudo -n /opt/wopr/deploy/firewall.sh enable` then power-cycle the ATA |

Never restart the daemon while someone is on the line: `grep -E "event=(call_start|call_end)" … | tail -1` should say `call_end`.

## During the event
- Flags so far: `ssh wopr-mini 'grep -c event=flag_unlocked /opt/wopr/log/wopr.log'`
- Live tail: `ssh wopr-mini 'tail -f /opt/wopr/log/wopr.log | grep -E "event=(stt|experience|flag_unlocked|call_end)"'`
- The hints recording is the help desk. If a caller is stuck, "who is on the line" and "how am I doing" are the two magic phrases.

## After the event
1. `D="/Volumes/ExternalBaseUnit/RP1-Recordings/hackoween-2026-10-30"; mkdir -p "$D/logs" && rsync -a wopr-mini:/opt/wopr/transcripts wopr-mini:/opt/wopr/recordings "$D/" && rsync -a wopr-mini:/opt/wopr/log/wopr.log "$D/logs/"`
2. `python3 tools/collect_index.py "$D" --since 2026-10-30` → CALLS.csv / CALLS.md / DAILY.md / README.md for the event day only
   (the log and transcripts on the mini still contain GrrCon; the date filter drops it).
3. `python3 analyze.py` for the funnel and completions.
