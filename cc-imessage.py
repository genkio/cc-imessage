#!/usr/bin/env python3
"""cc-imessage - a two-way bridge between Claude Code tmux sessions and iMessage.

You run many Claude Code sessions, each in its own tmux pane. This closes the
loop between them and your phone:

  notify  (outbound) A Stop hook pipes its JSON in. We pull the session's final
          reply, summarize it with Haiku, and queue an outbox request tagged
          [session:window]. The daemon does the actual send + guid capture.
  run     (inbound + sender) A daemon that (a) sends queued outbox requests via
          Messages and records the sent guid -> the tmux pane, and (b) polls for
          your replies and injects them (plus image paths) with tmux send-keys.
          A reply carries the guid of the message it answers, so it routes back
          to the exact pane; non-reply messages fall back to the last active one.

Why the split: reading chat.db and controlling Messages need TCC permissions
that macOS grants per code identity. Only the launchd-run daemon is its own
responsible process, so it holds them. `notify` runs under Claude Code (a
different responsible process) and would be denied, so it only summarizes and
queues - no chat.db, no AppleScript. All privileged work lives in the daemon.

Self-contained, stdlib only. macOS; the daemon needs Full Disk Access (read
chat.db) and Automation control of Messages (send).

  cc-imessage run                 # daemon (brew services runs this)
  cc-imessage notify              # outbound; reads Stop-hook JSON on stdin
  cc-imessage send --to H --text T
  cc-imessage poll                # debug: print new inbound messages as JSON
  cc-imessage map                 # debug: print the guid -> pane map
  cc-imessage enable|disable      # flip outbound (IMSG_OUT) without editing config
  cc-imessage status              # outbound on/off + daemon state
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".cc-imessage"
CONFIG = STATE_DIR / "config"
STATE = STATE_DIR / "state.json"
MAP = STATE_DIR / "threadmap.json"
LOCK = STATE_DIR / "threadmap.lock"
OUTBOX = STATE_DIR / "outbox"
CHAT_DB = HOME / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

MAP_CAP = 1000            # cap guid->pane map so it can't grow without bound
LAST_KEY = "__last__"     # fallback pane for non-reply messages
SKIP_ENV = "CC_IMESSAGE_SKIP"  # set on the nested summarizer so its Stop no-ops
OUTBOX_MAX_AGE = 300      # drop an unsendable outbox request after this many secs

CONFIG_TEMPLATE = """\
# cc-imessage config. PHONE is your phone's iMessage handle(s) as the Mac sees
# them. Comma-separate if your phone texts arrive under more than one address
# (e.g. both a number and an email): PHONE="+15551234567,you@icloud.com".
# The first entry is used as the send target. Empty = bridge disabled.
PHONE=""
# outbound: text a summary on each reply (1=on 0=off)
IMSG_OUT=1
# summarize replies longer than IMSG_SUMMARIZE_MIN chars via `claude -p haiku`
IMSG_SUMMARIZE=1
IMSG_SUMMARIZE_MIN=400
# inbound poll cadence, seconds
IMSG_POLL_INTERVAL=1.5
# route non-reply phone messages to the last active session (1=on 0=off)
IMSG_FALLBACK_LAST=1
"""

SUMMARY_PROMPT = (
    "Rewrite this AI coding assistant reply as a short phone notification I can "
    "read at a glance. At most 4 short sentences, plain English, no markdown or "
    "URLs. Cover what was done or found, key numbers or decisions, and anything I "
    "must do next. Lines like 'code block skipped' mean code is shown in the "
    "terminal; mention it only if it matters."
)

SEND_APPLESCRIPT = """\
on run argv
    if (count of argv) < 2 then error "need <handle> and <message text>"
    set targetHandle to item 1 of argv
    set messageText to item 2 of argv
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetHandle of targetService
        send messageText to targetBuddy
    end tell
end run
"""


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


def ensure_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        CONFIG.write_text(CONFIG_TEMPLATE)
        log(f"seeded {CONFIG} - set PHONE in it")


def load_config() -> dict:
    cfg = {}
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def config_handles(cfg: dict) -> list[str]:
    return [h.strip() for h in cfg.get("PHONE", "").split(",") if h.strip()]


def set_config_value(key: str, value: str) -> None:
    ensure_state()
    lines = CONFIG.read_text().splitlines()
    out, found = [], False
    for line in lines:
        s = line.strip()
        if not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    CONFIG.write_text("\n".join(out) + "\n")


def connect() -> sqlite3.Connection:
    if not CHAT_DB.exists():
        sys.exit(f"chat.db not found at {CHAT_DB}. Run on the Mac whose Messages "
                 f"you want, with Full Disk Access granted.")
    try:
        # mode=ro (not immutable): chat.db is WAL, and immutable=1 serves a frozen
        # snapshot that misses rows still in the write-ahead log until a checkpoint,
        # which made routing lag by minutes. ro reads committed WAL live.
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        sys.exit(f"could not open chat.db ({e}). Grant Full Disk Access.")
    conn.row_factory = sqlite3.Row
    return conn


def apple_time_to_iso(value) -> str | None:
    if not value:
        return None
    seconds = value / 1_000_000_000 if value > 1_000_000_000_000 else value
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone().isoformat()


def decode_attributed_body(blob: bytes) -> str | None:
    """Best-effort text from an NSAttributedString typedstream blob (macOS 13+)."""
    if not blob:
        return None
    try:
        i = blob.find(b"NSString")
        if i == -1:
            return None
        plus = blob.find(b"+", i)
        if plus == -1:
            return None
        j = plus + 1
        length = blob[j]
        j += 1
        if length == 0x81:
            length = int.from_bytes(blob[j:j + 2], "little"); j += 2
        elif length == 0x82:
            length = int.from_bytes(blob[j:j + 4], "little"); j += 4
        return blob[j:j + length].decode("utf-8", errors="replace").strip() or None
    except Exception:
        return None


def message_text(row: sqlite3.Row) -> str | None:
    if row["text"]:
        return row["text"]
    return decode_attributed_body(row["attributedBody"])


IMAGE_UTIS = {
    "public.jpeg", "public.png", "public.heic", "public.heif",
    "com.compuserve.gif", "public.webp", "public.tiff",
}


def resolve_attachments(conn, rowid: int, convert_heic: bool = False):
    rows = conn.execute(
        """
        SELECT a.filename, a.mime_type, a.uti
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id = ?
        """, (rowid,)).fetchall()
    out = []
    for r in rows:
        path = os.path.expanduser(r["filename"]) if r["filename"] else None
        exists = bool(path and os.path.exists(path))
        is_image = (r["uti"] in IMAGE_UTIS) or ((r["mime_type"] or "").startswith("image/"))
        rec = {"path": path, "exists": exists, "mime_type": r["mime_type"],
               "uti": r["uti"], "is_image": is_image}
        if convert_heic and exists and r["uti"] in ("public.heic", "public.heif"):
            rec["png_path"] = convert_to_png(path)
        out.append(rec)
    return out


def convert_to_png(src: str) -> str | None:
    dst = os.path.splitext(src)[0] + ".png"
    if os.path.exists(dst):
        return dst
    try:
        subprocess.run(["sips", "-s", "format", "png", src, "--out", dst],
                       check=True, capture_output=True)
        return dst if os.path.exists(dst) else None
    except Exception:
        return None


def load_last_rowid() -> int | None:
    if STATE.exists():
        try:
            return int(json.loads(STATE.read_text()).get("last_rowid"))
        except Exception:
            return None
    return None


def save_last_rowid(rowid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last_rowid": rowid}))


def _handle_in_clause(handles: list[str]) -> tuple[str, list[str]]:
    return ",".join("?" * len(handles)), list(handles)


def poll_new(handles: list[str], convert_heic: bool = True) -> list[dict]:
    """Inbound messages from any configured handle, newer than last poll.

    First run baselines at now so history isn't replayed.
    """
    # close every tick: mode=ro on a WAL db opens db+wal+shm fds, so a leaked
    # connection per poll exhausts the fd limit in ~2 min and wedges the daemon
    conn = connect()
    try:
        last = load_last_rowid()
        if last is None:
            last = conn.execute("SELECT MAX(ROWID) AS m FROM message").fetchone()["m"] or 0
            save_last_rowid(last)
            return []
        placeholders, hp = _handle_in_clause(handles)
        rows = conn.execute(
            f"""
            SELECT m.ROWID AS rowid, m.guid, m.text, m.attributedBody, m.date,
                   m.cache_has_attachments, m.thread_originator_guid, h.id AS handle
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.ROWID > ? AND m.is_from_me = 0 AND h.id IN ({placeholders})
            ORDER BY m.ROWID ASC
            """, [last, *hp]).fetchall()
        msgs = []
        for r in rows:
            atts = resolve_attachments(conn, r["rowid"], convert_heic) if r["cache_has_attachments"] else []
            msgs.append({
                "rowid": r["rowid"], "guid": r["guid"], "handle": r["handle"],
                "text": message_text(r), "date": apple_time_to_iso(r["date"]),
                "reply_to_guid": r["thread_originator_guid"], "attachments": atts,
            })
        new_max = max([last] + [r["rowid"] for r in rows]) if rows else last
        save_last_rowid(new_max)
        return msgs
    finally:
        conn.close()


def _applescript_path() -> str:
    p = STATE_DIR / "send.applescript"
    if not p.exists() or p.read_text() != SEND_APPLESCRIPT:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(SEND_APPLESCRIPT)
    return str(p)


def imessage_send(handle: str, text: str, timeout: float = 30.0) -> None:
    # timeout so a pending Automation-consent prompt can't hang the daemon forever
    subprocess.run(["osascript", _applescript_path(), handle, text],
                   check=True, capture_output=True, text=True, timeout=timeout)


def find_sent_guid(handles: list[str], text: str, timeout: float = 3.0) -> str | None:
    """The row we just sent appears in chat.db a beat later; return its guid."""
    placeholders, hp = _handle_in_clause(handles)
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = connect()
        rows = conn.execute(
            f"""
            SELECT m.guid, m.text, m.attributedBody
            FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.is_from_me = 1 AND h.id IN ({placeholders})
            ORDER BY m.ROWID DESC LIMIT 8
            """, hp).fetchall()
        conn.close()
        for r in rows:
            if (message_text(r) or "") == text:
                return r["guid"]
        time.sleep(0.3)
    return None


def _read_map() -> dict:
    try:
        return json.loads(MAP.read_text())
    except Exception:
        return {}


def update_map(guid: str, target: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        m = _read_map()
        m[LAST_KEY] = target
        if guid:
            m[guid] = target
        guids = [k for k in m if k != LAST_KEY]
        for stale in (guids[:-MAP_CAP] if len(guids) > MAP_CAP else []):
            del m[stale]
        tmp = MAP.with_suffix(".tmp")
        tmp.write_text(json.dumps(m))
        tmp.replace(MAP)
        fcntl.flock(lock, fcntl.LOCK_UN)


def tmux_alive(target: str) -> bool:
    # list-panes -t returns nonzero for a dead target; display-message -t
    # silently falls back to a default client and returns 0, so it can't be used
    return subprocess.run(["tmux", "list-panes", "-t", target],
                          capture_output=True).returncode == 0


def tmux_label(target: str) -> str:
    r = subprocess.run(["tmux", "display-message", "-p", "-t", target,
                        "#{session_name}:#{window_name}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "cc"


def inject(target: str, text: str) -> bool:
    if not tmux_alive(target):
        log(f"target gone, dropping: {target}")
        return False
    # text and Enter as separate calls; -l keeps chars literal (no key lookup)
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", text])
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"])
    return True


def message_payload(msg: dict) -> str:
    text = msg.get("text") or ""
    for att in msg.get("attachments") or []:
        if att.get("is_image") and att.get("exists"):
            path = att.get("png_path") or att.get("path")
            if path:
                text += ("  " if text else "") + path
    return text.strip()


def extract_reply(transcript_path: str) -> str:
    """Final reply = trailing assistant text after the last user-type entry.

    Tool results are user entries, so mid-turn text between tool calls is
    excluded; sidechain entries belong to subagents and are ignored.
    """
    try:
        lines = Path(transcript_path).read_text().splitlines()
    except Exception:
        return ""
    turns = []
    for line in lines:
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("isSidechain"):
            continue
        if o.get("type") in ("assistant", "user"):
            turns.append(o)
    last_user = max((i for i, o in enumerate(turns) if o.get("type") == "user"), default=-1)
    texts = []
    for o in turns[last_user + 1:]:
        content = (o.get("message") or {}).get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
    return "\n\n".join(texts).strip()


def poll_flush(transcript_path: str) -> str:
    """Stop fires before the final text entry is flushed; poll then settle."""
    text = ""
    for _ in range(15):
        text = extract_reply(transcript_path)
        if text:
            break
        time.sleep(0.2)
    if not text:
        return ""
    for _ in range(5):
        time.sleep(0.3)
        nxt = extract_reply(transcript_path)
        if nxt == text:
            break
        text = nxt
    return text


def strip_markdown(text: str) -> str:
    out, incode, intable = [], False, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            if incode:
                incode = False
            else:
                incode = True
                lang = s.lstrip("`").strip()
                out.append(f"{lang} code block skipped." if lang else "Code block skipped.")
            continue
        if incode:
            continue
        if s.startswith("|"):
            if not intable:
                out.append("Table skipped.")
                intable = True
            continue
        intable = False
        if re.match(r"^([-*_]\s*){3,}$", s):
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*>\s*", "", line)
        out.append(line)
    txt = "\n".join(out)
    txt = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", txt)
    txt = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", txt)
    txt = re.sub(r"https?://\S+", "link", txt)
    return txt.replace("**", "").replace("__", "").replace("`", "").strip()


def summarize(text: str, timeout: float = 30.0) -> str:
    if not shutil.which("claude"):
        return text
    env = dict(os.environ, **{SKIP_ENV: "1"})
    try:
        r = subprocess.run(["claude", "-p", "--model", "haiku", SUMMARY_PROMPT],
                           input=text, text=True, capture_output=True, timeout=timeout, env=env)
        return r.stdout.strip() or text
    except Exception:
        return text


def queue_outbox(target: str, label: str, text: str) -> None:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    # time_ns name keeps outbox processing in send order
    path = OUTBOX / f"{time.time_ns()}-{os.getpid()}.json"
    path.write_text(json.dumps({"target": target, "label": label, "text": text}))


def process_outbox(handles: list[str]) -> None:
    """Daemon side of outbound: send each queued request and map guid -> pane."""
    if not handles:
        return
    for f in sorted(OUTBOX.glob("*.json")):
        try:
            req = json.loads(f.read_text())
        except Exception:
            f.unlink(missing_ok=True)
            continue
        body = f"[{req.get('label', 'cc')}] {req.get('text', '')}"
        try:
            imessage_send(handles[0], body)
        except subprocess.TimeoutExpired:
            # consent prompt likely pending; retry next tick until it ages out
            if time.time() - f.stat().st_mtime > OUTBOX_MAX_AGE:
                log(f"outbox {f.name} timed out too long; dropping")
                f.unlink(missing_ok=True)
            continue
        except subprocess.CalledProcessError as e:
            log(f"send failed ({e.stderr.strip() if e.stderr else e}); dropping {f.name}")
            f.unlink(missing_ok=True)
            continue
        # delivered once send returns -> remove now so a later failure (e.g. guid
        # lookup denied when FDA is off) can't resend the same message every tick
        f.unlink(missing_ok=True)
        try:
            guid = find_sent_guid(handles, body)
        except Exception as e:
            guid = None
            log(f"guid lookup failed ({e}); reply-thread mapping skipped, fallback still works")
        update_map(guid or "", req.get("target", ""))
        log(f"sent outbox -> {req.get('target')} (guid={'yes' if guid else 'none'})")


def cmd_notify(args) -> int:
    # nested summarizer's own Stop hook -> do nothing (would recurse)
    if os.environ.get(SKIP_ENV):
        return 0
    ensure_state()
    try:
        hook = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    # continuation forced by a blocking stop hook (e.g. comments audit): meta-work
    if hook.get("stop_hook_active"):
        return 0

    cfg = load_config()
    if not config_handles(cfg) or cfg.get("IMSG_OUT", "1") != "1":
        return 0
    target = os.environ.get("TMUX_PANE")
    if not target:  # no pane -> a reply couldn't be routed back anyway
        return 0
    tp = hook.get("transcript_path")
    if not tp or not os.path.exists(tp):
        return 0

    text = poll_flush(tp)
    if not text:
        return 0
    speech = strip_markdown(text)
    if not speech:
        return 0
    if cfg.get("IMSG_SUMMARIZE", "1") == "1" and len(speech) > int(cfg.get("IMSG_SUMMARIZE_MIN", "400")):
        speech = summarize(speech)

    # queue only; the daemon (which holds the TCC grants) sends + maps the guid
    queue_outbox(target, tmux_label(target), speech)
    return 0


def cmd_run(args) -> int:
    ensure_state()
    cfg = load_config()
    handles = config_handles(cfg)
    interval = float(cfg.get("IMSG_POLL_INTERVAL", "1.5"))
    use_fallback = cfg.get("IMSG_FALLBACK_LAST", "1") == "1"
    if not handles:
        log("PHONE not set in config; daemon idle. Set it and restart the service.")
    log(f"cc-imessage daemon up (handles={handles or 'none'}, "
        f"interval={interval}s, fallback={'on' if use_fallback else 'off'})")

    fails = 0
    while True:
        ok = True
        try:
            if handles:
                process_outbox(handles)
                _tick(handles, use_fallback)
        except Exception as e:
            log(f"tick error: {e}")
            ok = False
        fails = 0 if ok else fails + 1
        time.sleep(interval if fails == 0 else min(interval * 2 ** fails, 30))


def _tick(handles: list[str], use_fallback: bool) -> None:
    m = _read_map()
    for msg in poll_new(handles, convert_heic=True):
        guid = msg.get("reply_to_guid")
        target = m.get(guid) if guid else (m.get(LAST_KEY) if use_fallback else None)
        if not target:
            log(f"no route for msg {msg.get('rowid')} (reply_to={guid}); skipping")
            continue
        payload = message_payload(msg)
        if not payload:
            continue
        if inject(target, payload):
            log(f"routed msg {msg.get('rowid')} -> {target}")


def cmd_send(args) -> int:
    ensure_state()
    handles = [args.to] if args.to else config_handles(load_config())
    if not handles:
        sys.exit("no handle: pass --to or set PHONE in config")
    body = args.text
    imessage_send(handles[0], body)
    guid = find_sent_guid(handles, body)
    if args.target:
        update_map(guid or "", args.target)
    print(json.dumps({"sent": True, "to": handles[0], "guid": guid}))
    return 0


def cmd_poll(args) -> int:
    ensure_state()
    handles = [args.handle] if args.handle else config_handles(load_config())
    if not handles:
        sys.exit("no handle: pass --handle or set PHONE in config")
    print(json.dumps(poll_new(handles, convert_heic=args.convert_heic), indent=2))
    return 0


def cmd_map(args) -> int:
    print(json.dumps(_read_map(), indent=2))
    return 0


def cmd_enable(args) -> int:
    # IMSG_OUT gates notify, read fresh each reply, so this needs no restart
    set_config_value("IMSG_OUT", "1")
    print("outbound enabled")
    return 0


def cmd_disable(args) -> int:
    set_config_value("IMSG_OUT", "0")
    print("outbound disabled (summaries + Haiku both skipped)")
    return 0


def cmd_status(args) -> int:
    ensure_state()
    cfg = load_config()
    handles = config_handles(cfg)
    running = subprocess.run(["pgrep", "-f", "cc-imessage run"], capture_output=True).returncode == 0
    print(f"outbound: {'on' if cfg.get('IMSG_OUT', '1') == '1' else 'off'}")
    print(f"handles:  {', '.join(handles) if handles else '(none set)'}")
    print(f"daemon:   {'running' if running else 'not running'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cc-imessage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="daemon: send queued outbox + inject phone replies into tmux")
    r.set_defaults(func=cmd_run)

    n = sub.add_parser("notify", help="outbound: read Stop-hook JSON on stdin, queue a summary")
    n.set_defaults(func=cmd_notify)

    s = sub.add_parser("send", help="send an iMessage; prints the sent guid")
    s.add_argument("--to")
    s.add_argument("--text", required=True)
    s.add_argument("--target", help="tmux pane to map the guid to (for reply routing)")
    s.set_defaults(func=cmd_send)

    pl = sub.add_parser("poll", help="debug: print new inbound messages as JSON")
    pl.add_argument("--handle")
    pl.add_argument("--convert-heic", action="store_true")
    pl.set_defaults(func=cmd_poll)

    mp = sub.add_parser("map", help="debug: print the guid -> pane map")
    mp.set_defaults(func=cmd_map)

    sub.add_parser("enable", help="turn outbound summaries on").set_defaults(func=cmd_enable)
    sub.add_parser("disable", help="turn outbound summaries off").set_defaults(func=cmd_disable)
    sub.add_parser("status", help="show outbound on/off + daemon state").set_defaults(func=cmd_status)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
