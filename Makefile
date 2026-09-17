# ReadyPhreakerOne — all mini-side work runs over SSH. Set MINI=<ssh alias> in .env.local
-include .env.local
export MINI

REMOTE_DIR := ~/ReadyPhreakerOne
REMOTE_ENV := export PATH=/opt/homebrew/bin:$$PATH
DEV_PORT   := 9093
PROD_PORT  := 9092
PY         := venv/bin/python

.PHONY: check-mini sync probe test harness provision verify deploy-daemon logs local-test local-harness

check-mini:
	@if [ -z "$(MINI)" ]; then echo "MINI is unset. Put 'MINI=wopr-mini' in .env.local"; exit 1; fi

sync: check-mini
	rsync -a --delete --exclude venv --exclude clips --exclude .git --exclude __pycache__ ./ $(MINI):$(REMOTE_DIR)/

probe: check-mini
	ssh $(MINI) '$(REMOTE_ENV); echo USER=$$(whoami); uname -m; sw_vers | tr "\n" " "; echo; echo MEM_GB=$$(( $$(sysctl -n hw.memsize) / 1073741824 )); sysctl -n machdep.cpu.brand_string; brew --version | head -1; python3.14 --version; ollama list 2>/dev/null || echo "ollama not responding"; for b in asterisk espeak-ng; do printf "%s=%s\n" $$b "$$(command -v $$b || echo none)"; done'

test: check-mini
	ssh $(MINI) '$(REMOTE_ENV); cd $(REMOTE_DIR) && $(PY) -m unittest discover -s tests -v'

# caffeinate keeps the mini awake for the run (its idle sleep is 1 min until provisioning fixes pmset)
HARNESS_ARGS ?=
harness: check-mini sync
	ssh $(MINI) '$(REMOTE_ENV); cd $(REMOTE_DIR) && (caffeinate -i -s $(PY) wopr_daemon.py --stub-llm --port $(DEV_PORT) --stats-port 9193 > /tmp/wopr-dev.log 2>&1 & echo $$! > /tmp/wopr-dev.pid); for i in $$(seq 1 60); do nc -z 127.0.0.1 $(DEV_PORT) 2>/dev/null && break; sleep 1; done; $(PY) harness.py synth >/dev/null && caffeinate -i -s $(PY) harness.py run --port $(DEV_PORT) --stats-port 9193 $(HARNESS_ARGS); rc=$$?; kill $$(cat /tmp/wopr-dev.pid) 2>/dev/null; exit $$rc'

provision: check-mini sync
	ssh -t $(MINI) '$(REMOTE_ENV); cd $(REMOTE_DIR) && ./deploy/provision.sh $(PROVISION_ARGS)'

verify: check-mini
	ssh -t $(MINI) '$(REMOTE_ENV); cd $(REMOTE_DIR) && ./deploy/provision.sh --verify'

deploy-daemon: check-mini sync
	ssh -t $(MINI) '$(REMOTE_ENV); cd $(REMOTE_DIR) && cp wopr_daemon.py /opt/wopr/wopr_daemon.py && (cmp -s requirements.txt /opt/wopr/requirements.txt || (cp requirements.txt /opt/wopr/ && /opt/wopr/venv/bin/pip install -q -r /opt/wopr/requirements.txt)) && sudo launchctl kickstart -k system/com.crunchwhistle.wopr && echo restarted'

logs: check-mini
	ssh $(MINI) 'tail -f /opt/wopr/log/wopr.log'

# Local (dev box) equivalents
local-test:
	$(PY) -m unittest discover -s tests -v

local-harness:
	@($(PY) wopr_daemon.py --stub-llm --port $(DEV_PORT) --stats-port 9193 > /tmp/wopr-dev.log 2>&1 & echo $$! > /tmp/wopr-dev.pid); \
	for i in $$(seq 1 60); do nc -z 127.0.0.1 $(DEV_PORT) 2>/dev/null && break; sleep 1; done; \
	$(PY) harness.py synth >/dev/null && $(PY) harness.py run --port $(DEV_PORT) --stats-port 9193 $(HARNESS_ARGS); rc=$$?; kill $$(cat /tmp/wopr-dev.pid) 2>/dev/null; exit $$rc
