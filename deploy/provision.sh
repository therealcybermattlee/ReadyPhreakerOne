#!/usr/bin/env bash
# CrunchWhistle — provision a headless Apple Silicon Mac mini from "Homebrew only" to a running
# payphone stack: Asterisk (source build), Ollama, espeak-ng, the WOPR daemon, models, three
# LaunchDaemons, and host settings. Idempotent: every step checks its postcondition first.
#
#   ./deploy/provision.sh [--preflight] [--verify] [--skip-asterisk] [--skip-models]
#                         [--asterisk-version 22] [--model qwen3:8b]
#
# Run as the admin user over `ssh -t` (sudo prompts once). Exit 0 ok · 1 step failed · 2 preconditions.
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SVC_USER="$(whoami)"
SVC_HOME="$HOME"
WOPR_DIR=/opt/wopr
AST_DIR=/opt/asterisk
BUILD_DIR="$SVC_HOME/asterisk-build"
AST_MAJOR="${ASTERISK_VERSION:-22}"
MODEL="${WOPR_MODEL:-qwen3:8b}"
WHISPER="${WOPR_WHISPER_MODEL:-base.en}"
OLLAMA_MIN="0.33"
LABELS=(com.crunchwhistle.ollama com.crunchwhistle.asterisk com.crunchwhistle.wopr)

PREFLIGHT=0; VERIFY=0; SKIP_AST=0; SKIP_MODELS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --preflight) PREFLIGHT=1 ;;
    --verify) VERIFY=1 ;;
    --skip-asterisk) SKIP_AST=1 ;;
    --skip-models) SKIP_MODELS=1 ;;
    --asterisk-version) AST_MAJOR="$2"; shift ;;
    --model) MODEL="$2"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
done_() { log "DONE  $*"; }
skip() { log "SKIP  $*"; }
fail() { log "FAIL  $*"; exit 1; }

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# ─────────────────────────────────────────────────────────────────────────────────────────
# Preflight
# ─────────────────────────────────────────────────────────────────────────────────────────
preflight() {
  local arch mem os brew clt
  arch=$(uname -m); mem=$(( $(sysctl -n hw.memsize) / 1073741824 )); os=$(sw_vers -productVersion)
  brew=$(brew --prefix 2>/dev/null || true); clt=$(xcode-select -p 2>/dev/null || true)
  log "host: $(sysctl -n machdep.cpu.brand_string), $mem GB, macOS $os, arch $arch, user $SVC_USER"
  log "brew: ${brew:-MISSING}   xcode CLT: ${clt:-MISSING}   python3.14: $(python3.14 --version 2>/dev/null || echo MISSING)"
  log "ollama: $(ollama --version 2>/dev/null | tr -d '\n' || echo none)   asterisk: $($AST_DIR/sbin/asterisk -V 2>/dev/null || echo none)   espeak-ng: $(espeak-ng --version 2>/dev/null | head -1 || echo none)"
  log "sleep: $(pmset -g | awk '/^ *sleep/{print $2}')   firewall: $(/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate | tr -d '\n')"
  [ "$arch" = arm64 ] || { log "not Apple Silicon"; exit 2; }
  [ -n "$brew" ] || { log "Homebrew missing (https://brew.sh)"; exit 2; }
  [ -n "$clt" ] || { log "Xcode Command Line Tools missing: run xcode-select --install"; exit 2; }
  if [ "$mem" -lt 16 ] && [ "$MODEL" = "qwen3:8b" ]; then
    log "WARN  only $mem GB RAM: switching default model to llama3.2:3b"; MODEL=llama3.2:3b
  fi
}

# ─────────────────────────────────────────────────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────────────────────────────────────────────────
step_brew() {
  local pkgs="espeak-ng jansson libxml2 pkg-config libedit python@3.14 sqlite openssl@3 ollama" missing=""
  for p in $pkgs; do brew list --formula "$p" >/dev/null 2>&1 || missing="$missing $p"; done
  if [ -n "$missing" ]; then brew install -q $missing; done_ "brew install$missing"; else skip "brew packages present"; fi
  local ov; ov=$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 || echo 0)
  if version_ge "$ov" "$OLLAMA_MIN"; then skip "ollama $ov ≥ $OLLAMA_MIN"; else brew upgrade -q ollama; done_ "ollama upgraded from $ov"; fi
}

step_dirs() {
  if [ -d "$WOPR_DIR/log" ] && [ -d "$WOPR_DIR/hf" ] && [ -d "$AST_DIR" ] && [ "$(stat -f %Su "$WOPR_DIR")" = "$SVC_USER" ]; then
    skip "directories"
  else
    sudo mkdir -p "$WOPR_DIR/log" "$WOPR_DIR/hf" "$AST_DIR"
    sudo chown -R "$SVC_USER:staff" "$WOPR_DIR" "$AST_DIR"
    done_ "directories $WOPR_DIR $AST_DIR owned by $SVC_USER"
  fi
  # a hand-started `ollama serve` would hold 11434 and block the LaunchDaemon
  if ! sudo launchctl print system/com.crunchwhistle.ollama >/dev/null 2>&1; then
    local pid; pid=$(pgrep -f "ollama serve" || true)
    if [ -n "$pid" ]; then kill $pid || true; sleep 1; done_ "stopped hand-started ollama serve ($pid)"; fi
  fi
}

ast_installed_ok() { "$AST_DIR/sbin/asterisk" -V 2>/dev/null | grep -q "Asterisk $AST_MAJOR\."; }

step_asterisk_build() {
  if ast_installed_ok; then skip "asterisk $("$AST_DIR/sbin/asterisk" -V) installed"; return; fi
  mkdir -p "$BUILD_DIR"; cd "$BUILD_DIR"
  local tar="asterisk-$AST_MAJOR-current.tar.gz"
  [ -f "$tar" ] || curl -sSL -o "$tar" "https://downloads.asterisk.org/pub/telephony/asterisk/$tar"
  local src; src=$(ls -d asterisk-$AST_MAJOR.*/ 2>/dev/null | head -1 || true)
  if [ -z "$src" ]; then tar xzf "$tar"; src=$(ls -d asterisk-$AST_MAJOR.*/ | head -1); fi
  cd "$src"
  if [ ! -x main/asterisk ]; then
    log "building Asterisk in $BUILD_DIR/$src (10–20 min)"
    ./configure --prefix="$AST_DIR" --with-pjproject-bundled --with-jansson-bundled \
      --with-ssl="$(brew --prefix openssl@3)" CFLAGS="-I$(brew --prefix)/include" LDFLAGS="-L$(brew --prefix)/lib" \
      > "$BUILD_DIR/configure.log" 2>&1 || { tail -30 "$BUILD_DIR/configure.log"; fail "configure (see $BUILD_DIR/configure.log)"; }
    make menuselect.makeopts > "$BUILD_DIR/menuselect.log" 2>&1
    menuselect/menuselect --enable app_audiosocket --enable res_audiosocket --enable func_uuid \
      --enable chan_pjsip --enable codec_ulaw --disable BUILD_NATIVE menuselect.makeopts
    caffeinate -i -s make -j"$(sysctl -n hw.ncpu)" > "$BUILD_DIR/make.log" 2>&1 || { tail -40 "$BUILD_DIR/make.log"; fail "make (see $BUILD_DIR/make.log)"; }
  fi
  [ -f apps/app_audiosocket.so ] && [ -f res/res_audiosocket.so ] || fail "audiosocket modules did not build"
  make install > "$BUILD_DIR/install.log" 2>&1 || { tail -30 "$BUILD_DIR/install.log"; fail "make install"; }
  if [ ! -f "$AST_DIR/etc/asterisk/asterisk.conf" ]; then make samples > "$BUILD_DIR/samples.log" 2>&1; fi
  cp menuselect.makeopts "$REPO_DIR/deploy/asterisk/menuselect.makeopts.built" 2>/dev/null || true
  ast_installed_ok || fail "installed asterisk does not report major $AST_MAJOR"
  done_ "asterisk built and installed: $("$AST_DIR/sbin/asterisk" -V)"
}

step_asterisk_config() {
  local etc="$AST_DIR/etc/asterisk" changed=0
  mkdir -p "$etc"
  for f in asterisk.conf modules.conf pjsip.conf extensions.conf rtp.conf; do
    local rendered; rendered=$(sed "s|__USER__|$SVC_USER|g" "$REPO_DIR/deploy/asterisk/$f")
    if [ ! -f "$etc/$f" ] || [ "$(cat "$etc/$f")" != "$rendered" ]; then printf '%s\n' "$rendered" > "$etc/$f"; changed=1; fi
  done
  if [ ! -f "$etc/pjsip_secret.conf" ]; then
    local secret; secret=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
    printf 'password = %s\n' "$secret" > "$etc/pjsip_secret.conf"; chmod 600 "$etc/pjsip_secret.conf"
    changed=1
    log "==============================================================================="
    log "  HT802 SIP password for user 'payphone' (shown once; stored in $etc/pjsip_secret.conf):"
    log "      $secret"
    log "  Enter it in the ATA per deploy/ht802-settings.md"
    log "==============================================================================="
  fi
  [ "$changed" = 1 ] && done_ "asterisk configs installed" || skip "asterisk configs current"
  if sudo launchctl print system/com.crunchwhistle.asterisk >/dev/null 2>&1 && [ "$changed" = 1 ]; then
    "$AST_DIR/sbin/asterisk" -rx "core reload" >/dev/null 2>&1 || true
  fi
}

step_venv() {
  local changed=0
  if [ ! -x "$WOPR_DIR/venv/bin/python" ]; then python3.14 -m venv "$WOPR_DIR/venv"; "$WOPR_DIR/venv/bin/pip" install -q --upgrade pip; changed=1; fi
  if ! cmp -s "$REPO_DIR/requirements.txt" "$WOPR_DIR/requirements.txt" 2>/dev/null; then
    "$WOPR_DIR/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"; cp "$REPO_DIR/requirements.txt" "$WOPR_DIR/"; changed=1
  fi
  if ! cmp -s "$REPO_DIR/wopr_daemon.py" "$WOPR_DIR/wopr_daemon.py" 2>/dev/null; then cp "$REPO_DIR/wopr_daemon.py" "$WOPR_DIR/"; changed=1; fi
  cp "$REPO_DIR/harness.py" "$WOPR_DIR/" 2>/dev/null || true
  [ -f "$WOPR_DIR/wopr.env" ] || { cp "$REPO_DIR/deploy/wopr.env.example" "$WOPR_DIR/wopr.env"; changed=1; }
  [ "$changed" = 1 ] && done_ "venv + daemon at $WOPR_DIR" || skip "venv + daemon current"
}

step_models() {
  # ollama needs a server; use the LaunchDaemon if loaded, else a temporary one
  local tmp=0
  if ! curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null; then
    (OLLAMA_HOST=127.0.0.1:11434 nohup ollama serve >/dev/null 2>&1 &); tmp=1
    for i in $(seq 1 30); do curl -s -m 1 http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
  fi
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then skip "ollama model $MODEL present"; else ollama pull "$MODEL"; done_ "ollama pull $MODEL"; fi
  [ "$tmp" = 1 ] && { pkill -f "ollama serve" || true; }
  if HF_HOME="$WOPR_DIR/hf" HF_HUB_OFFLINE=1 "$WOPR_DIR/venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER', device='cpu', compute_type='int8')" >/dev/null 2>&1; then
    skip "whisper $WHISPER cached in $WOPR_DIR/hf"
  else
    HF_HOME="$WOPR_DIR/hf" "$WOPR_DIR/venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER', device='cpu', compute_type='int8')" \
      || fail "whisper download"
    done_ "whisper $WHISPER downloaded to $WOPR_DIR/hf"
  fi
}

step_launchd() {
  local any=0
  for label in "${LABELS[@]}"; do
    [ "$SKIP_AST" = 1 ] && [ "$label" = com.crunchwhistle.asterisk ] && continue
    local src="$REPO_DIR/deploy/launchd/$label.plist" dst="/Library/LaunchDaemons/$label.plist"
    local rendered; rendered=$(sed -e "s|__USER__|$SVC_USER|g" -e "s|__HOME__|$SVC_HOME|g" "$src")
    if [ -f "$dst" ] && [ "$(cat "$dst")" = "$rendered" ] && sudo launchctl print "system/$label" >/dev/null 2>&1; then
      skip "launchd $label"; continue
    fi
    printf '%s\n' "$rendered" | sudo tee "$dst" >/dev/null
    sudo chown root:wheel "$dst"; sudo chmod 644 "$dst"
    sudo launchctl bootout "system/$label" >/dev/null 2>&1 || true
    sudo launchctl bootstrap system "$dst"
    done_ "launchd $label bootstrapped"; any=1
  done
  [ "$any" = 1 ] && sleep 3 || true
}

step_host() {
  if [ "$(pmset -g | awk '/^ *sleep/{print $2}')" = 0 ] && [ "$(pmset -g | awk '/^ *autorestart/{print $2}')" = 1 ]; then
    skip "pmset"
  else
    sudo pmset -a sleep 0 disksleep 0 autorestart 1; done_ "pmset: never sleep, auto-restart after power loss"
  fi
  local fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if [ "$SKIP_AST" = 0 ] && [ -x "$AST_DIR/sbin/asterisk" ]; then
    if sudo $fw --listapps 2>/dev/null | grep -q "$AST_DIR/sbin/asterisk"; then skip "firewall rule for asterisk"; else
      sudo $fw --add "$AST_DIR/sbin/asterisk" >/dev/null; sudo $fw --unblockapp "$AST_DIR/sbin/asterisk" >/dev/null; done_ "firewall allows asterisk"; fi
  fi
  local rendered; rendered=$(sed "s|__USER__|$SVC_USER|g" "$REPO_DIR/deploy/newsyslog-wopr.conf")
  if [ -f /etc/newsyslog.d/wopr.conf ] && [ "$(cat /etc/newsyslog.d/wopr.conf)" = "$rendered" ]; then skip "newsyslog"; else
    printf '%s\n' "$rendered" | sudo tee /etc/newsyslog.d/wopr.conf >/dev/null; done_ "newsyslog rotation"; fi
}

# ─────────────────────────────────────────────────────────────────────────────────────────
# Verify
# ─────────────────────────────────────────────────────────────────────────────────────────
verify() {
  local pass=0 total=0
  check() { total=$((total+1)); if eval "$2" >/dev/null 2>&1; then pass=$((pass+1)); log "PASS  $1"; else log "FAIL  $1"; fi; }
  check "1 preflight (arm64, brew, CLT)" "[ \$(uname -m) = arm64 ] && brew --prefix && xcode-select -p"
  check "2 brew packages" "for p in espeak-ng jansson libxml2 pkg-config libedit python@3.14 ollama; do brew list --formula \$p; done && version_ge \$(ollama --version | grep -oE '[0-9]+\.[0-9]+' | head -1) $OLLAMA_MIN"
  check "3 directories owned by $SVC_USER" "[ \$(stat -f %Su $WOPR_DIR) = $SVC_USER ] && [ -d $WOPR_DIR/log ] && [ -d $WOPR_DIR/hf ]"
  if [ "$SKIP_AST" = 0 ]; then
    check "4 asterisk $AST_MAJOR with audiosocket modules" "ast_installed_ok && [ \$($AST_DIR/sbin/asterisk -rx 'module show like audiosocket' | grep -c 'Running') -ge 2 ] && $AST_DIR/sbin/asterisk -rx 'module show like func_uuid' | grep -q Running"
    check "5 asterisk config (endpoint payphone, ext 2600/2601/2602)" "$AST_DIR/sbin/asterisk -rx 'pjsip show endpoints' | grep -q payphone && $AST_DIR/sbin/asterisk -rx 'dialplan show payphone' | grep -q 2600 && $AST_DIR/sbin/asterisk -rx 'dialplan show payphone' | grep -q 2602"
  else
    log "SKIP  4-5 asterisk (--skip-asterisk)"
  fi
  check "6 venv + daemon --check" "$WOPR_DIR/venv/bin/python -c 'import faster_whisper, numpy' && (set -a; . $WOPR_DIR/wopr.env; $WOPR_DIR/venv/bin/python $WOPR_DIR/wopr_daemon.py --check)"
  check "7 models (ollama $MODEL, whisper $WHISPER offline)" "ollama list | awk '{print \$1}' | grep -qx $MODEL && HF_HOME=$WOPR_DIR/hf HF_HUB_OFFLINE=1 $WOPR_DIR/venv/bin/python -c \"from faster_whisper import WhisperModel; WhisperModel('$WHISPER', device='cpu', compute_type='int8')\""
  local want="com.crunchwhistle.ollama com.crunchwhistle.wopr"; [ "$SKIP_AST" = 0 ] && want="$want com.crunchwhistle.asterisk"
  check "8 launchd services running ($want)" "for l in $want; do sudo launchctl print system/\$l | grep -q 'state = running'; done"
  check "9 host settings (pmset, newsyslog)" "[ \$(pmset -g | awk '/^ *sleep/{print \$2}') = 0 ] && [ -f /etc/newsyslog.d/wopr.conf ]"
  check "10 end-to-end harness against 9092" "cd $REPO_DIR && ([ -f clips/manifest.json ] || $WOPR_DIR/venv/bin/python harness.py synth) && $WOPR_DIR/venv/bin/python harness.py run --port 9092 --only clean_2600,hangup"
  log "$pass/$total checks passed"
  [ "$pass" = "$total" ]
}

# ─────────────────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────────────────
preflight
[ "$PREFLIGHT" = 1 ] && exit 0
if [ "$VERIFY" = 1 ]; then verify; exit $?; fi

sudo -v || { log "sudo required"; exit 2; }
( while true; do sudo -n true; sleep 50; done ) 2>/dev/null &
SUDO_KEEPALIVE=$!
trap 'kill $SUDO_KEEPALIVE 2>/dev/null || true' EXIT

step_brew
step_dirs
if [ "$SKIP_AST" = 0 ]; then step_asterisk_build; step_asterisk_config; else log "SKIP  asterisk (--skip-asterisk)"; fi
step_venv
if [ "$SKIP_MODELS" = 0 ]; then step_models; else log "SKIP  models (--skip-models)"; fi
step_launchd
step_host
log "provisioning complete — running verify"
verify
