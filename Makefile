.PHONY: run poll map send
.DEFAULT_GOAL := run

LOG := $(HOME)/.cc-imessage/bridge.log

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
