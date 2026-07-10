#!/usr/bin/env python3
"""ccim-launcher - frozen TCC anchor for cc-imessage.

macOS keys Full Disk Access and Automation grants to the code identity of the
launchd job's binary, and every cc-imessage release is a new identity (a
self-signed cert with a stable designated requirement does NOT carry grants
across builds; tested, see HANDOVER). That forced a re-grant ritual on every
upgrade.

launchd runs THIS binary instead. It never changes, and it runs the real
daemon as a child, so all privileged work in the child tree (chat.db reads,
Messages AppleScript) is attributed to the launcher's stable identity.

Grant FDA + Automation to this binary once; upgrade cc-imessage freely.

A version bump here costs every user a re-grant, so this file stays minimal
and is meant to change ~never. Anything that could want iteration belongs in
cc-imessage itself.
"""

import os
import subprocess
import sys
import time

__version__ = "1.0.0"

DEFAULT_DAEMON = "/opt/homebrew/opt/cc-imessage/bin/cc-imessage"


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"ccim-launcher {__version__}")
        return 0
    daemon = os.environ.get("CCIM_DAEMON", DEFAULT_DAEMON)
    backoff = 1
    while True:
        started = time.time()
        try:
            subprocess.run([daemon, "run"])
        except FileNotFoundError:
            print(f"ccim-launcher: daemon not found at {daemon}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"ccim-launcher: {e}", file=sys.stderr, flush=True)
        # the daemon loops forever, so any exit is abnormal: restart it, backing
        # off only if it is thrashing (launchd's keep_alive guards the launcher)
        backoff = 1 if time.time() - started > 60 else min(backoff * 2, 30)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
