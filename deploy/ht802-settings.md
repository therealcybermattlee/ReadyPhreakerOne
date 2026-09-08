# Grandstream HT802 settings for the CrunchWhistle payphone

Apply these through the ATA's web interface (`http://<ata-ip>`, admin login). Everything is on
**FXS Port 1** unless noted. Settings not listed keep their defaults. After **Apply** on each
page, do a final **Reboot** from the top menu.

The SIP password is printed exactly once by `deploy/provision.sh` (it is also stored in
`/opt/asterisk/etc/asterisk/pjsip_secret.conf` on the mini, mode 0600).

## Profile 1 (Profiles → Profile 1)

| Setting | Value | Why |
|---|---|---|
| Primary SIP Server | `<mini LAN IP>` (currently 10.17.13.134) | Asterisk on the mini |
| Outbound Proxy | *(blank)* | same LAN, no proxy |
| SIP Transport | UDP | matches `pjsip.conf` transport |
| Register Expiration | 1 (minute) | fast re-registration after ATA/mini reboot |
| Local SIP Port | 5060 | default |
| Use Random Port | No | predictable |
| NAT Traversal | No | same LAN |
| **Preferred Vocoder — choice 1 through 8** | **PCMU** in every slot | μ-law end to end, nothing else offered |
| Silence Suppression | **No** | in-band tone must survive |
| Voice Frames per TX | 2 | 20 ms packets, matches AudioSocket framing |
| DTMF Method | RFC2833 | out-of-band, keeps voice path clean |
| Use First Matching Vocoder in 200OK SDP | Yes | belt and braces for PCMU |
| Jitter Buffer Type | Fixed | steady latency for the tone detector |
| Jitter Buffer Length | Low | minimum delay |
| Send Hook Flash Event | No | a coin phone's hook switch must not flash-transfer |
| **Offhook Auto-Dial** (optional, recommended) | `2600` | lifting the handset goes straight to WOPR; leave blank to dial manually |
| Offhook Auto-Dial Delay | 0 | immediate |

## FXS Port 1 (FXS Ports → Port 1)

| Setting | Value |
|---|---|
| SIP User ID | `payphone` |
| Authenticate ID | `payphone` |
| Authenticate Password | *(from provisioning output)* |
| Name | `Payphone` |
| Profile ID | Profile 1 |
| Hunting Group | None |
| Enable Port | Yes |

## Advanced Settings (Advanced Settings page)

| Setting | Value | Why |
|---|---|---|
| **Echo Cancellation** | **Disabled** (per-port "Echo Cancellation" on the FXS page on newer firmware) | the daemon relies on the whistle arriving untouched; it runs its own sidetone guard |
| Silence Suppression / VAD | Disabled | as above |
| Gain: TX / RX | 0 dB / 0 dB | tune later only if the handset is too quiet for STT |
| Enable Hook Flash | No | |
| Ring Frequency / Ring Voltage | defaults (20 Hz / 40 Vrms) | fine for Western Electric and most coin sets |
| NTP Server | `<mini LAN IP>` or your router | correct timestamps in the ATA logs |

## Verify from the mini

```bash
sudo /opt/asterisk/sbin/asterisk -rx "pjsip show contacts"        # payphone/sip:... Avail  <n> ms
sudo /opt/asterisk/sbin/asterisk -rx "pjsip show endpoint payphone" | head -30
sudo /opt/asterisk/sbin/asterisk -rx "pjsip set logger on"         # watch REGISTER/INVITE if not registering
```

Lift the handset and dial **2601**: you hear yourself echoed. **2602**: steady 1 kHz test tone.
While a call is up:

```bash
sudo /opt/asterisk/sbin/asterisk -rx "core show channels verbose"   # shows ulaw on the channel
```

Then **2600** (or just lift the handset if auto-dial is set): silence. Whistle 2600 Hz.

## If the ATA won't register

- `pjsip set logger on` and look for `401 Unauthorized` → password mismatch; re-enter it.
- No REGISTER at all → check the SIP server IP, that the ATA and mini share the LAN/VLAN, and
  that nothing on the mini's firewall blocks UDP 5060 (provisioning adds an allow rule).
- Registers but no audio → RTP range 10000–10100 must reach the mini; confirm `rtp.conf` and
  that the ATA has NAT traversal off.

## Isolated phone-island addressing (2026-09-08)

When the mini and ATA sit on a dumb switch with no uplink, both take static IPs on
`192.168.50.0/24`:

| Device | Setting | Value |
|---|---|---|
| Mini en0 | static | `192.168.50.1` / `255.255.255.0`, no gateway (Wi-Fi carries the default route) |
| HT802 | Basic Settings → IP Address | Statically configured: `192.168.50.2`, mask `255.255.255.0`, gateway blank (or `192.168.50.1`) |
| HT802 | FXS Port 1 → Primary SIP Server | `192.168.50.1` |

Configure the ATA's IP and SIP server **while it is still on the main network** (reachable at its
current address), Save and Apply, then move its cable to the dumb switch. After that its web UI is
only reachable from the island (e.g. a laptop plugged into the switch at `192.168.50.9`).
