# cc-imessage

A two-way bridge between Claude Code tmux sessions and iMessage. Each Claude
session texts you a Haiku-summarized notification when it finishes, and you reply
from your phone to drive that exact session, hands-free, from anywhere.

It closes the loop: outbound summaries go to your phone, and your phone replies
land back in the right tmux pane as if you had typed them.

## How it works

Two subcommands, split by the macOS permissions each needs:

- **`notify`** (outbound, runs as a Claude Code Stop hook). Reads the session's
  final reply from the transcript, strips code/tables, summarizes it with
  `claude -p --model haiku`, and queues a request tagged `[session:window]`. It
  needs no special permissions, so it works even though Claude Code isn't the
  process macOS granted access to.
- **`run`** (the daemon). Does the privileged work: it (a) sends queued requests
  via Messages and records each sent message's `guid` against the tmux pane it
  came from, and (b) polls `chat.db` for your replies and injects them with
  `tmux send-keys`.

`brew services` doesn't run the daemon directly: it runs **`ccim-launcher`**, a
tiny frozen binary that starts the daemon as its child. macOS attributes the
child's privileged work to the launcher's never-changing code identity, so you
grant Full Disk Access and Automation to the launcher once and they survive
every cc-imessage upgrade. (Self-signed certs with a stable designated
requirement do not carry TCC grants across builds; we tested.)

The messaging medium sits behind a small transport interface (`send`,
`find_sent_id`, `poll`); iMessage is the built-in default, and a different
medium (a bot API, say) is one new transport class that needs no Apple
permissions at all.

When you long-press a summary and tap **Reply**, that reply carries the `guid` of
what it answered, so it routes back to the exact pane, no prefixes to type. A
message that isn't a reply falls back to the last active session (toggleable).
Image attachments are resolved on disk (HEIC converted to PNG) and their paths
injected so Claude Code attaches them.

The Mac and the phone must use **separate iMessage accounts** (e.g. Mac on an
iCloud address, phone on a different Apple ID). That way your texts to the Mac
arrive as received (`is_from_me = 0`) and are cleanly separated from the
summaries the Mac sends.

## Install

```sh
brew install genkio/tap/cc-imessage
brew services start genkio/tap/cc-imessage   # the daemon
```

### Required setup (one time)

1. **Config.** First run seeds `~/.cc-imessage/config`. Set `PHONE` to how your
   phone's iMessages arrive at the Mac, the address shown on the phone under
   Settings > Messages > Send & Receive > "Start new conversations from" (a phone
   number in E.164 like `+15551234567`, or an Apple ID email). If they can arrive
   under more than one address, comma-separate them: `PHONE="+1555…,you@me.com"`.
2. **Full Disk Access** so the daemon can read `chat.db`. Grant it to the
   launcher (its own code identity, nothing else inherits it):
   `/opt/homebrew/var/cc-imessage/ccim-launcher`
   (System Settings > Privacy & Security > Full Disk Access > `+`, Cmd+Shift+G to
   type the path). Then `brew services restart genkio/tap/cc-imessage`.
3. **Automation.** The first outbound send pops "ccim-launcher wants to control
   Messages", click **Allow**. That's the send permission, also scoped to the
   launcher.
4. **Outbound hook.** Add a Claude Code Stop hook. The guard makes it a harmless
   no-op on machines where cc-imessage isn't installed yet:
   ```json
   { "hooks": { "Stop": [ { "hooks": [
     { "type": "command",
       "command": "command -v cc-imessage >/dev/null 2>&1 && cc-imessage notify || true" }
   ] } ] } }
   ```

Grants persist across `brew upgrade` because they attach to the launcher, whose
bytes never change between cc-imessage releases (it lives under Homebrew's
`var/`, untouched by upgrades). You grant FDA + Automation once per machine; a
re-grant is only ever needed if the launcher itself releases a new version,
which is intended to be ~never.

### Restoring on a new machine

Safe to restore your dotfiles (with the Stop hook) before installing cc-imessage.
Until it's installed the hook is a no-op (the `command -v` guard); once installed
but not yet configured, `notify` returns early (no `PHONE`). Nothing queues or
errors in the meantime, no backlog builds up. Outbound simply starts working for
new replies once you finish the setup above; inbound needs `brew services start`.

## Usage

```sh
cc-imessage run                      # daemon, foreground
cc-imessage notify                   # outbound; reads Stop-hook JSON on stdin
cc-imessage send --to +1555… --text "hi" [--target %12]
cc-imessage poll [--handle +1555…]   # debug: print new inbound messages
cc-imessage map                      # debug: print the guid -> pane map
cc-imessage enable | disable         # flip outbound (IMSG_OUT); no restart needed
cc-imessage status                   # outbound on/off + daemon state
```

`send`/`poll` touch `chat.db`, so run them where FDA applies (the daemon's
context); from a plain shell they'll be denied unless that shell has FDA.

Make targets wrap the script for local runs:

```sh
make build           # compile the cc-imessage binary
make build-launcher  # compile ccim-launcher; ONLY when its version bumps (costs everyone a re-grant)
make run             # daemon; append to ~/.cc-imessage/bridge.log, open lnav if present
make poll
make map
make send TO=+1555… TEXT="hi"
```

## Config

`~/.cc-imessage/config` (shell syntax):

| key | default | meaning |
|-----|---------|---------|
| `PHONE` | (empty) | your phone's handle(s), comma-separated; empty disables the bridge |
| `IMSG_OUT` | 1 | send a summary on each reply (0 disables outbound; takes effect next reply, no restart) |
| `IMSG_SUMMARIZE` | 1 | rewrite each reply for speech with Haiku |
| `IMSG_POLL_INTERVAL` | 1.5 | inbound poll cadence, seconds |
| `IMSG_FALLBACK_LAST` | 1 | route non-reply messages to the last active session |
| `CCIM_TRANSPORT` | imessage | messaging medium (only `imessage` is built in today) |

## Limitations

- macOS only, tmux only. Sessions outside tmux can't be routed to.
- Prebuilt binaries are arm64; on Intel, `make build` + `make build-launcher`
  and grant FDA/Automation to your local launcher build.
- Text extraction covers ordinary messages; rich/edited messages may need
  `imessage-exporter` for perfect fidelity.
- Sends via AppleScript; if it errors, use the full E.164 handle and make sure
  the recipient is a known iMessage contact.
