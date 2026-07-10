.PHONY: run poll map send build build-launcher
.DEFAULT_GOAL := run

LOG := $(HOME)/.cc-imessage/bridge.log

# Standalone binary. Holds no TCC grants itself (ccim-launcher does), so no
# signing needed; rebuild freely per release.
build:
	python3 -m venv build/venv
	build/venv/bin/pip install -q --upgrade pip pyinstaller
	build/venv/bin/pyinstaller --onefile --name cc-imessage --clean --noconfirm cc-imessage.py

# The frozen TCC anchor. Do NOT rebuild per release: new bytes = new code
# identity = every user re-grants FDA + Automation. Build only when
# ccim-launcher.py itself changes (bump its __version__), then cut a
# launcher-vX.Y.Z release for the formula to pin.
build-launcher:
	python3 -m venv build/venv
	build/venv/bin/pip install -q --upgrade pip pyinstaller
	build/venv/bin/pyinstaller --onefile --name ccim-launcher --clean --noconfirm ccim-launcher.py

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
