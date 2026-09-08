#!/usr/bin/env bash
# CrunchWhistle firewall control. Run with sudo on the mini.
#   sudo ./deploy/firewall.sh enable   # install rules + turn pf on, with a dead-man
#   sudo ./deploy/firewall.sh keep      # cancel the dead-man once you've confirmed SSH still works
#   sudo ./deploy/firewall.sh persist   # install the boot-time LaunchDaemon
#   sudo ./deploy/firewall.sh disable   # turn pf off and cancel the dead-man
#   sudo ./deploy/firewall.sh status
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/pf-crunchwhistle.conf
SRC="$HERE/pf-crunchwhistle.conf"
PID=/var/run/crunch-pf-deadman.pid
DEADMAN="${DEADMAN:-180}"

case "${1:-status}" in
  enable)
    cp "$SRC" "$CONF"
    pfctl -n -f "$CONF"                         # validate; aborts on error
    echo "Ruleset valid. Enabling pf with a ${DEADMAN}s dead-man safety."
    nohup sh -c "sleep $DEADMAN; pfctl -d 2>/dev/null; echo fired-$(date -u +%H:%M:%S) >> /var/log/crunch-pf-deadman.log" >/dev/null 2>&1 &
    echo $! > "$PID"
    pfctl -e -f "$CONF" 2>&1 | grep -vi "already enabled" || true
    echo
    echo "pf is ON. From ANOTHER machine confirm SSH still works:  ssh wopr-mini true && echo OK"
    echo "Then keep it:            sudo $0 keep"
    echo "If you get locked out, do nothing — pf auto-disables in ${DEADMAN}s."
    ;;
  keep)
    [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null || true
    rm -f "$PID"
    echo "Dead-man cancelled; ruleset stays active. Run 'sudo $0 persist' to survive reboot."
    ;;
  persist)
    cp "$HERE/launchd/com.crunchwhistle.pf.plist" /Library/LaunchDaemons/
    chown root:wheel /Library/LaunchDaemons/com.crunchwhistle.pf.plist
    chmod 644 /Library/LaunchDaemons/com.crunchwhistle.pf.plist
    launchctl bootout system/com.crunchwhistle.pf 2>/dev/null || true
    launchctl bootstrap system /Library/LaunchDaemons/com.crunchwhistle.pf.plist
    echo "Boot-time pf loader installed."
    ;;
  disable)
    [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null || true
    rm -f "$PID"
    pfctl -d 2>/dev/null || true
    echo "pf disabled."
    ;;
  status)
    pfctl -s info 2>/dev/null | head -1 || true
    echo "--- rules ---"; pfctl -s rules 2>/dev/null || echo "pf not active"
    ;;
  *) echo "usage: $0 {enable|keep|persist|disable|status}"; exit 2;;
esac
