#!/usr/bin/env bash
# Put the mini's wired port (en0) onto the isolated phone island, keeping Wi-Fi as the
# primary interface for management + internet. Run with sudo on the mini.
#   sudo ./deploy/island-net.sh island   # en0 -> 192.168.50.1/24, Wi-Fi primary
#   sudo ./deploy/island-net.sh revert    # en0 back to DHCP on the main LAN
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 2; }
ETH="${ETH:-Ethernet}"; WIFI="${WIFI:-Wi-Fi}"
IP="${ISLAND_IP:-192.168.50.1}"; MASK=255.255.255.0

order_wifi_first() { networksetup -ordernetworkservices "$WIFI" "$ETH" "Thunderbolt Bridge" 2>/dev/null \
                     || networksetup -ordernetworkservices "$WIFI" "$ETH"; }

case "${1:-status}" in
  island)
    order_wifi_first                                   # default route must prefer Wi-Fi
    networksetup -setmanual "$ETH" "$IP" "$MASK" "$IP" # router=self: on-link only, not a default route
    sleep 2
    echo "en0 ($ETH) = $(ipconfig getifaddr en0 2>/dev/null)"
    echo "en1 (Wi-Fi) = $(ipconfig getifaddr en1 2>/dev/null)"
    DR=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
    echo "default route via: $DR"
    [ "$DR" = en1 ] && echo "OK: default route is Wi-Fi (management + internet safe)" \
                    || echo "WARNING: default route is $DR, expected en1 — internet/mgmt may break"
    ;;
  revert)
    networksetup -setdhcp "$ETH"; order_wifi_first
    echo "en0 back to DHCP: $(sleep 2; ipconfig getifaddr en0 2>/dev/null || echo none)"
    ;;
  status)
    echo "en0=$(ipconfig getifaddr en0 2>/dev/null) en1=$(ipconfig getifaddr en1 2>/dev/null) default via $(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
    ;;
  *) echo "usage: $0 {island|revert|status}"; exit 2;;
esac
