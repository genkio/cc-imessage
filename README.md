# cc-imessage

A two-way bridge between Claude Code tmux sessions and iMessage. Each Claude
session texts you a Haiku-summarized notification when it finishes, and you reply
from your phone to drive that exact session, hands-free, from anywhere.

It closes the loop: outbound summaries go to your phone, and your phone replies
land back in the right tmux pane as if you had typed them.

## How it works

Two directions, each picked for what it does best:

- **Outbound (`notify`).** A Claude Code Stop hook pipes its JSON to
  `cc-imessage notify`. We read the session's final reply from the transcript,
  strip code/tables, summarize it with `claude -p --model haiku`, and text it to
  your phone tagged `[session:window]`. The sent message's `guid` is recorded
  against the tmux pane it came from.
- **Inbound (`run`).** A daemon polls the Messages database (`chat.db`,
  read-only) for your replies. When you long-press a summary and tap **Reply**,
  that reply carries the `guid` of what it answered, so we look up the pane and
  inject your text with `tmux send-keys`. No prefixes to type. Image attachments
  are resolved on disk (HEIC converted to PNG) and their paths are injected so
  Claude Code attaches them.

Routing is by reply-thread, so several sessions can ping you at once and each
reply lands where you were actually responding. A message that isn't a reply
falls back to the last active session (toggleable).

Because the Mac and phone use separate iMessage accounts, your commands arrive
as received (`is_from_me = 0`) and are cleanly separated from the summaries the
Mac sent.

## Install

Homebrew, no clone needed:

```sh
brew install genkio/tap/cc-imessage
```

Then:

```sh
brew services start genkio/tap/cc-imessage   # inbound daemon in the background
```

Or clone this repo and run the script directly (see Usage).

### Required setup

1. **Full Disk Access** for whatever runs the daemon, so it can read `chat.db`:
   System Settings > Privacy & Security > Full Disk Access. For `brew services`
   (launchd) add `/usr/bin/python3`; for a foreground run add your terminal app.
2. **Config.** First run seeds `~/.cc-imessage/config`. Set `PHONE` to your
   iPhone's iMessage handle in E.164 (`+15551234567`).
3. **Outbound hook.** Add a Claude Code Stop hook so each session notifies you:
   ```json
   { "hooks": { "Stop": [ { "hooks": [
     { "type": "command", "command": "cc-imessage notify" }
   ] } ] } }
   ```

## Usage

```sh
cc-imessage run                     # inbound daemon, foreground
cc-imessage notify                  # outbound; reads Stop-hook JSON on stdin
cc-imessage send --to +1555... --text "hi" [--target %12]
cc-imessage poll [--handle +1555...] # debug: print new inbound messages
cc-imessage map                     # debug: print the guid -> pane map
```

Make targets wrap the script:

```sh
make run      # daemon; append to ~/.cc-imessage/bridge.log, open lnav if present
make poll     # print new inbound messages
make map      # print the routing map
make send TO=+1555... TEXT="hi"
```

## Config

`~/.cc-imessage/config` (shell syntax):

| key | default | meaning |
|-----|---------|---------|
| `PHONE` | (empty) | your iMessage handle; empty disables the bridge |
| `IMSG_OUT` | 1 | send a summary on each reply |
| `IMSG_SUMMARIZE` | 1 | summarize replies over the threshold with Haiku |
| `IMSG_SUMMARIZE_MIN` | 400 | char threshold for summarizing |
| `IMSG_POLL_INTERVAL` | 1.5 | inbound poll cadence, seconds |
| `IMSG_FALLBACK_LAST` | 1 | route non-reply messages to the last active session |

## Limitations

- macOS only, tmux only. Sessions outside tmux can't be routed to.
- Full Disk Access is the usual reason reads fail; grant it to the process that
  runs the daemon (the interpreter under launchd, or your terminal).
- Text extraction covers ordinary messages; rich/edited messages may need
  `imessage-exporter` for perfect fidelity.
- Sends via AppleScript; the `buddy` form works on most macOS versions. If it
  errors, use the full E.164 handle and make sure the recipient is a known
  iMessage contact.
