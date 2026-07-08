.PHONY: run poll map send build
.DEFAULT_GOAL := run

LOG := $(HOME)/.cc-imessage/bridge.log
SIGN_ID ?= cc-imessage-selfsign

# Compile to a standalone binary so it has its own code identity -> Full Disk
# Access can be granted to just cc-imessage, not the shared python. Then sign
# with a self-signed cert so the designated requirement keys on the cert
# (stable) instead of the per-build cdhash -> TCC grants survive brew upgrades.
build:
	python3 -m venv build/venv
	build/venv/bin/pip install -q --upgrade pip pyinstaller
	build/venv/bin/pyinstaller --onefile --name cc-imessage --clean --noconfirm cc-imessage.py
	@if security find-identity -v 2>/dev/null | grep -q "$(SIGN_ID)"; then \
		codesign --force --sign "$(SIGN_ID)" --identifier com.genkio.cc-imessage dist/cc-imessage && \
		echo "signed dist/cc-imessage with $(SIGN_ID)"; \
	else \
		echo "no '$(SIGN_ID)' identity; dist/cc-imessage left adhoc (TCC grants won't persist across upgrades)"; \
	fi

run:
	@if command -v lnav >/dev/null 2>&1; then \
		./cc-imessage.py run 2>&1 | tee -a "$(LOG)" | lnav; \
	else \
		echo "lnav not installed; logging to $(LOG) (Ctrl-C to stop)"; \
		./cc-imessage.py run 2>&1 | tee -a "$(LOG)"; \
	fi

poll:
	./cc-imessage.py poll

map:
	./cc-imessage.py map

# usage: make send TO=+15551234567 TEXT="hello"
send:
	./cc-imessage.py send --to "$(TO)" --text "$(TEXT)"
