#!/usr/bin/env python3
"""Loopback HTTP server behind the LaTeX overlay.

Serves ``index-overlay.html`` plus the vendored KaTeX assets, bridges the
Wayland clipboard through ``cliphist`` and ``wl-copy``, and exposes a
read-only view over the local OpenCode database and the session registry
written by the ``latex-overlay-sessions.js`` plugin.

I/O: binds ``127.0.0.1:17832``; shells out to ``cliphist``, ``wl-paste`` and
``wl-copy``; opens ``opencode.db`` in SQLite ``mode=ro``.

Side effects: writes ``trash.json``, ``suppress.json`` and
``session-history.json`` under ``$XDG_STATE_HOME/latex-overlay`` and unlinks
dead or stale registry files under the plugin registry directory.
"""
import contextlib
import hashlib
import http.server
import json
import mimetypes
import os
import re
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

PORT = 17832
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "latex-overlay")
TRASH_FILE = os.path.join(STATE_DIR, "trash.json")
TRASH_LOCK = threading.RLock()
# Short-lived marker consumed by cliphist-guard so an overlay copy is not
# stored by the wl-paste --watch watcher.  Must outlive the watcher latency.
SUPPRESS_FILE = os.path.join(STATE_DIR, "suppress.json")
SUPPRESS_TTL = 2.0

# ── opencode session overlay (Fase 1b) ──
# The overlay page is served from these two origins; now that the server can
# expose whole conversations, CORS must not be a wildcard.
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:17832", "http://localhost:17832"})
OPENCODE_DB = os.environ.get(
    "OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db"))
# Registry written by the latex-overlay-sessions.js plugin, one file per pid.
# Must mirror the plugin's resolution order exactly.
def default_registry_dir():
    """Plugin registry directory: runtime dir when present, else the state dir."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "latex-overlay", "sessions")
    return os.path.join(STATE_DIR, "sessions")


REGISTRY_DIR = default_registry_dir()
REGISTRY_TTL = 24 * 60 * 60  # seconds; older proc files are pruned
SESSION_HISTORY_FILE = os.path.join(STATE_DIR, "session-history.json")
SESSION_HISTORY_MAX = 50
SESSION_HISTORY_LOCK = threading.RLock()

MAX_SESSION_MESSAGES = 50
MAX_SESSION_BYTES = 300_000
SESSION_QUERY_MULTIPLIER = 4  # fetch a margin so skipped (tool-only) msgs don't starve
MAX_SESSIONS_LIMIT = 1000
DEFAULT_SESSIONS_LIMIT = 100

MARKER_OPEN = "\uE000"
MARKER_CLOSE = "\uE001"


# ── Pure helpers (unit-testable, no I/O) ──

def origin_allowed(origin):
    """True when the request Origin is one the overlay page may use."""
    return bool(origin) and origin in ALLOWED_ORIGINS


def sanitize_text(value):
    """Strip the private marker code points the renderer reserves internally."""
    if not isinstance(value, str):
        return ""
    return value.replace(MARKER_OPEN, "").replace(MARKER_CLOSE, "")


_SENTENCE_RE = re.compile(r"(?s)^(.*?[.!?\u2026])(?=\s|$)")


def first_sentence(text, max_len=240):
    """Opening fragment of a user message, used as the blockquote reminder."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    match = _SENTENCE_RE.match(text)
    fragment = match.group(1).strip() if match else text
    if len(fragment) > max_len:
        fragment = fragment[: max_len - 1].rstrip() + "\u2026"
    return fragment


def parse_message_role(data):
    """Role from a v1 ``message`` row payload, or ``""`` when absent."""
    if isinstance(data, dict):
        obj = data
    else:
        try:
            obj = json.loads(data)
        except (TypeError, ValueError):
            return ""
    if not isinstance(obj, dict):
        return ""
    role = obj.get("role")
    return role if isinstance(role, str) else ""


def part_text(data):
    """Return the text of a `text` part, else None (reasoning/tool excluded)."""
    if isinstance(data, dict):
        obj = data
    else:
        try:
            obj = json.loads(data)
        except (TypeError, ValueError):
            return None
    if not isinstance(obj, dict) or obj.get("type") != "text":
        return None
    text = obj.get("text")
    return sanitize_text(text) if isinstance(text, str) else ""


def combine_text_parts(part_datas, separator="\n\n"):
    """Join the text of several v1 parts, skipping reasoning and tool parts."""
    chunks = []
    for data in part_datas:
        text = part_text(data)
        if text:
            chunks.append(text)
    return separator.join(chunks).strip()


def parse_registry_timestamp(value):
    """Epoch seconds from an ISO string or an epoch number (ms if large)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 1e11 else ts
    if isinstance(value, str):
        try:
            return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
    return None


def _pid_alive(pid):
    """True when the registry entry's pid still exists in /proc."""
    return isinstance(pid, int) and os.path.isdir(f"/proc/{pid}")


def load_registry(directory=None, now=None):
    """Read live plugin registry files, pruning dead/stale ones in place."""
    directory = directory if directory is not None else REGISTRY_DIR
    now = time.time() if now is None else now
    entries = []
    try:
        names = os.listdir(directory)
    except OSError:
        return entries
    for name in names:
        if not (name.startswith("proc-") and name.endswith(".json")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as stream:
                entry = json.load(stream)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        updated_at = parse_registry_timestamp(entry.get("updated_at"))
        stale = updated_at is None or now - updated_at > REGISTRY_TTL
        if not _pid_alive(entry.get("pid")) or stale:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        if not isinstance(entry.get("sessions"), dict):
            entry["sessions"] = {}
        entries.append(entry)
    return entries


def registry_index(entries):
    """Collapse the per-process registries into {sessionID: open info}."""
    index = {}
    for entry in entries:
        pid = entry.get("pid")
        tty = entry.get("tty")
        active = entry.get("active_session")
        for session_id, rec in (entry.get("sessions") or {}).items():
            if not isinstance(session_id, str):
                continue
            rec = rec if isinstance(rec, dict) else {}
            info = index.setdefault(session_id, {
                "open": True, "pid": pid, "tty": tty,
                "state": rec.get("state") or "idle", "active": False,
                "parent_id": rec.get("parent_id"),
            })
            if rec.get("state") == "busy":
                info["state"] = "busy"
            if info.get("pid") is None:
                info["pid"] = pid
            if info.get("tty") is None:
                info["tty"] = tty
        if isinstance(active, str):
            info = index.setdefault(active, {
                "open": True, "pid": pid, "tty": tty, "state": "idle",
                "active": True, "parent_id": None,
            })
            info["active"] = True
    return index


@contextlib.contextmanager
def open_db(db_path=None):
    """Context manager yielding a read-only connection with a query-only guard."""
    db_path = db_path if db_path is not None else OPENCODE_DB
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=3000")
        yield connection
    finally:
        connection.close()


def _table_exists(connection, name):
    """True when ``name`` is a table in the attached database."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def parse_v2_message(msg_type, data):
    """Parse an opencode v2 session_message row into (role, text)."""
    try:
        obj = json.loads(data) if not isinstance(data, dict) else data
    except (TypeError, ValueError):
        return "", None
    if not isinstance(obj, dict):
        return "", None
    if msg_type == "user":
        text = obj.get("text")
        return "user", sanitize_text(text) if isinstance(text, str) else ""
    if msg_type == "assistant":
        chunks = []
        content = obj.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(sanitize_text(text))
        return "assistant", "\n\n".join(chunks).strip()
    return "", None


def query_session_rows(db_path=None, include_subagents=False):
    """All non-archived sessions with first/last message timestamps.

    OpenCode 2.0 moved live sessions to session_v2/session_message; the v1
    session/message tables keep stale pre-upgrade rows. Prefer v2 when present.
    """
    with open_db(db_path) as connection:
        use_v2 = _table_exists(connection, "session_v2")
        table = "session_v2" if use_v2 else "session"
        msg_table = "session_message" if _table_exists(connection, "session_message") else "message"
        sql = (
            "SELECT s.id, s.title, s.agent, s.directory, s.parent_id, "
            "s.time_created, s.time_updated, MIN(m.time_created), MAX(m.time_created), "
            "p.title AS parent_title "
            f"FROM {table} s LEFT JOIN {msg_table} m ON m.session_id = s.id "
            f"LEFT JOIN {table} p ON p.id = s.parent_id "
            "WHERE s.time_archived IS NULL"
        )
        if not include_subagents:
            sql += " AND s.parent_id IS NULL"
        sql += " GROUP BY s.id"
        rows = connection.execute(sql).fetchall()
    return [{
        "id": row[0], "title": row[1], "agent": row[2], "directory": row[3],
        "parent_id": row[4], "time_created": row[5], "time_updated": row[6],
        "first_msg_at": row[7], "last_msg_at": row[8], "parent_title": row[9],
    } for row in rows]


def merge_session_rows(db_rows, registry, include_subagents=False, limit=None, history=None):
    """Merge DB sessions with the live registry into the /sessions payload."""
    history = history or {}
    merged = []
    for row in db_rows:
        session_id = row.get("id")
        if not session_id:
            continue
        parent_id = row.get("parent_id")
        if parent_id and not include_subagents:
            continue
        info = registry.get(session_id) or {}
        is_open = bool(info.get("open"))
        merged.append({
            "id": session_id,
            "title": sanitize_text(row.get("title") or "") or "(sin título)",
            "agent": sanitize_text(row.get("agent") or ""),
            "directory": sanitize_text(row.get("directory") or ""),
            "parent_id": parent_id,
            "parent_title": sanitize_text(row.get("parent_title") or ""),
            "time_created": row.get("time_created"),
            "time_updated": row.get("time_updated"),
            "first_msg_at": row.get("first_msg_at"),
            "last_msg_at": row.get("last_msg_at"),
            "open": is_open,
            "pid": info.get("pid") if is_open else None,
            "tty": info.get("tty") if is_open else None,
            "state": (info.get("state") or "idle") if is_open else None,
            "active": bool(info.get("active")) if is_open else False,
            "previously_opened": bool(session_id in history) or is_open,
            "last_opened_at": history.get(session_id),
        })
    # Open sessions first, then most recently updated.
    merged.sort(key=lambda item: (0 if item["open"] else 1, -(item["time_updated"] or 0)))
    if limit is not None:
        merged = merged[: max(0, limit)]
    return merged


def query_session_detail(db_path=None, session_id=None, max_messages=MAX_SESSION_MESSAGES,
                         max_bytes=MAX_SESSION_BYTES):
    """Chronological text-only conversation for one session (newest kept)."""
    if not session_id:
        return None
    with open_db(db_path) as connection:
        use_v2 = _table_exists(connection, "session_v2")
        session_row = None
        if use_v2:
            session_row = connection.execute(
                "SELECT id, title, agent, directory, parent_id, time_created, time_updated "
                "FROM session_v2 WHERE id = ?", (session_id,)).fetchone()
        if session_row is None:
            session_row = connection.execute(
                "SELECT id, title, agent, directory, parent_id, time_created, time_updated "
                "FROM session WHERE id = ?", (session_id,)).fetchone()
            if session_row is None:
                return None
            return _query_session_detail_v1(
                connection, session_row, session_id, max_messages, max_bytes)
        if _table_exists(connection, "session_message"):
            return _query_session_detail_v2(
                connection, session_row, session_id, max_messages, max_bytes)
        return _query_session_detail_v1(
            connection, session_row, session_id, max_messages, max_bytes)


def _query_session_detail_v2(connection, session_row, session_id,
                             max_messages, max_bytes):
    """OpenCode 2.0 path: single session_message table, newest kept."""
    fetch_limit = max_messages * SESSION_QUERY_MULTIPLIER
    rows = connection.execute(
        "SELECT id, type, time_created, data FROM session_message WHERE session_id = ? "
        "ORDER BY seq DESC, id DESC LIMIT ?",
        (session_id, fetch_limit)).fetchall()
    collected = []
    bytes_total = 0
    truncated = len(rows) >= fetch_limit
    for row in rows:  # newest first
        if len(collected) >= max_messages:
            truncated = True
            break
        role, text = parse_v2_message(row[1], row[3])
        if not text:
            continue
        role = role or "assistant"
        if collected and bytes_total + len(text) > max_bytes:
            truncated = True
            break
        if len(text) > max_bytes:
            text = text[:max_bytes]
            truncated = True
        collected.append({
            "role": role,
            "text": text,
            "preview": first_sentence(text) if role == "user" else "",
            "time_created": row[2],
        })
        bytes_total += len(text)
    collected.reverse()
    return {
        "id": session_row[0],
        "title": sanitize_text(session_row[1] or "") or "(sin título)",
        "agent": sanitize_text(session_row[2] or ""),
        "directory": sanitize_text(session_row[3] or ""),
        "parent_id": session_row[4],
        "time_created": session_row[5],
        "time_updated": session_row[6],
        "messages": collected,
        "truncated": truncated,
    }


def _query_session_detail_v1(connection, session_row, session_id,
                             max_messages, max_bytes):
    """OpenCode 1.x path: message rows plus their text ``part`` rows."""
    fetch_limit = max_messages * SESSION_QUERY_MULTIPLIER
    message_rows = connection.execute(
        "SELECT id, time_created, data FROM message WHERE session_id = ? "
        "ORDER BY time_created DESC, id DESC LIMIT ?",
        (session_id, fetch_limit)).fetchall()
    parts_by_message = {}
    if message_rows:
        ids = [row[0] for row in message_rows]
        placeholders = ",".join("?" * len(ids))
        part_rows = connection.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? "
            f"AND message_id IN ({placeholders}) "
            "AND json_extract(data, '$.type') = 'text' "
            "ORDER BY message_id, id",
            [session_id, *ids]).fetchall()
        for message_id, data in part_rows:
            parts_by_message.setdefault(message_id, []).append(data)

    collected = []
    bytes_total = 0
    truncated = len(message_rows) >= fetch_limit
    for row in message_rows:  # newest first
        if len(collected) >= max_messages:
            truncated = True
            break
        role = parse_message_role(row[2])
        text = combine_text_parts(parts_by_message.get(row[0], []))
        if not text:
            continue
        if collected and bytes_total + len(text) > max_bytes:
            truncated = True
            break
        if len(text) > max_bytes:
            text = text[:max_bytes]
            truncated = True
        collected.append({
            "role": role or "assistant",
            "text": text,
            "preview": first_sentence(text) if role == "user" else "",
            "time_created": row[1],
        })
        bytes_total += len(text)

    collected.reverse()
    return {
        "id": session_row[0],
        "title": sanitize_text(session_row[1] or "") or "(sin título)",
        "agent": sanitize_text(session_row[2] or ""),
        "directory": sanitize_text(session_row[3] or ""),
        "parent_id": session_row[4],
        "time_created": session_row[5],
        "time_updated": session_row[6],
        "messages": collected,
        "truncated": truncated,
    }


def read_session_history(path=None):
    """{session_id: last_opened_epoch}; an empty dict when unreadable."""
    path = path if path is not None else SESSION_HISTORY_FILE
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_session_history(history, path=None):
    """Atomically replace the history file (tmp + fsync + rename)."""
    path = path if path is not None else SESSION_HISTORY_FILE
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="session-history.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(history, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Handler(http.server.BaseHTTPRequestHandler):
    """Request handler for the overlay's clipboard and session API."""

    def cors_origin(self):
        """Echo the request Origin only when it is on the allow-list."""
        try:
            origin = self.headers.get("Origin")
        except AttributeError:
            origin = None
        return origin if origin_allowed(origin) else None

    def send_cors_header(self):
        """Emit CORS headers for allowed origins, nothing otherwise."""
        origin = self.cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def reply_json(self, value, status=200):
        """Write ``value`` as a non-cached JSON response."""
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_cors_header()
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def history_lines(self):
        """``cliphist list`` lines that carry the ``id<TAB>preview`` separator."""
        result = subprocess.run(["cliphist", "list"], capture_output=True, timeout=2)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return [line for line in result.stdout.splitlines() if b"\t" in line]

    def selected_line(self, item_id):
        """The raw list line whose cliphist id matches ``item_id``."""
        return next((line for line in self.history_lines()
                     if line.split(b"\t", 1)[0].decode("ascii", "ignore") == item_id), None)

    def read_trash(self):
        """Trash entries as a list; a missing file means an empty trash."""
        try:
            with open(TRASH_FILE, encoding="utf-8") as stream:
                value = json.load(stream)
            return value if isinstance(value, list) else []
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("No se pudo leer la papelera") from exc

    def write_trash(self, entries):
        """Atomically persist the trash entries (tmp + fsync + rename)."""
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="trash.", dir=STATE_DIR, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(entries, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, TRASH_FILE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def put_clipboard(self, content):
        """Place ``content`` on the Wayland clipboard via ``wl-copy``."""
        result = subprocess.run(["wl-copy", "--type", "text/plain"], input=content.encode(),
                                capture_output=True, timeout=2)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))

    def write_suppress_marker(self, content):
        """Record the hash and timestamp that cliphist-guard consumes."""
        # Atomic write (same pattern as write_trash): the watcher guard must
        # never observe a half-written marker.
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        marker = {"sha256": hashlib.sha256(content.encode()).hexdigest(), "at": time.time()}
        fd, temporary = tempfile.mkstemp(prefix="suppress.", dir=STATE_DIR, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(marker, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, SUPPRESS_FILE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear_suppress_marker(self):
        """Drop the suppression marker, tolerating an absent file."""
        try:
            os.unlink(SUPPRESS_FILE)
        except OSError:
            pass

    def copy_excluded(self, content):
        """Copy to the clipboard without letting cliphist store it."""
        # Mark first, then copy.  If wl-copy fails there is nothing to suppress,
        # so drop the marker instead of eating an unrelated later copy.
        self.write_suppress_marker(content)
        try:
            self.put_clipboard(content)
        except Exception:
            self.clear_suppress_marker()
            raise

    def restore_entry(self, item_id):
        """Put a trashed entry back on the clipboard and drop it from trash."""
        with TRASH_LOCK:
            entries = self.read_trash()
            entry = next((item for item in entries if item.get("id") == item_id), None)
            if entry is None:
                return None
            self.put_clipboard(entry.get("content", ""))
            self.write_trash([item for item in entries if item.get("id") != item_id])
            return entry

    def record_session_open(self, session_id):
        """Remember that ``session_id`` was opened, capped at SESSION_HISTORY_MAX."""
        with SESSION_HISTORY_LOCK:
            history = read_session_history()
            history[session_id] = time.time()
            if len(history) > SESSION_HISTORY_MAX:
                ordered = sorted(history.items(), key=lambda item: item[1], reverse=True)
                history = dict(ordered[:SESSION_HISTORY_MAX])
            write_session_history(history)
            return history

    def handle_sessions(self, query):
        """``GET /sessions``: DB sessions merged with live registry state."""
        include_subagents = query.get("subagents", ["0"])[0] not in ("0", "", "false", "False")
        try:
            limit = int(query.get("limit", [str(DEFAULT_SESSIONS_LIMIT)])[0])
        except (TypeError, ValueError):
            limit = DEFAULT_SESSIONS_LIMIT
        limit = max(1, min(limit, MAX_SESSIONS_LIMIT))
        registry = registry_index(load_registry())
        rows = query_session_rows(include_subagents=include_subagents)
        history = read_session_history()
        sessions = merge_session_rows(rows, registry, include_subagents, limit, history)
        self.reply_json(sessions)

    def handle_session(self, query):
        """``GET /session``: text-only transcript for one session id."""
        session_id = query.get("id", [""])[0]
        if not session_id:
            return self.reply_json({"error": "Falta el parámetro id"}, 400)
        detail = query_session_detail(session_id=session_id)
        if detail is None:
            return self.reply_json({"error": "Sesión no encontrada"}, 404)
        self.record_session_open(session_id)
        self.reply_json(detail)

    def serve_vendor(self, path):
        """Serve a file under ``vendor/``, refusing anything outside that root."""
        vendor_root = os.path.realpath(os.path.join(SCRIPT_DIR, "vendor"))
        target = os.path.realpath(os.path.join(vendor_root, path[len("/vendor/"):]))
        if not target.startswith(vendor_root + os.sep) or not os.path.isfile(target):
            return self.send_error(404)
        with open(target, "rb") as stream:
            content = stream.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        """Route the read-only endpoints and fall back to the overlay page."""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/clipboard":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_cors_header()
                self.end_headers()
                result = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=1)
                self.wfile.write(result.stdout)
            elif parsed.path == "/history":
                self.reply_json([{"id": line.split(b"\t", 1)[0].decode("ascii", "replace"),
                                  "preview": line.split(b"\t", 1)[1].decode("utf-8", "replace")}
                                 for line in self.history_lines()])
            elif parsed.path == "/history/get":
                line = self.selected_line(query.get("id", [""])[0])
                if line is None:
                    return self.reply_json({"error": "History entry not found"}, 404)
                result = subprocess.run(["cliphist", "decode"], input=line + b"\n", capture_output=True, timeout=2)
                if result.returncode:
                    return self.reply_json({"error": result.stderr.decode("utf-8", "replace")}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_cors_header()
                self.end_headers()
                self.wfile.write(result.stdout)
            elif parsed.path == "/history/delete":
                item_id = query.get("id", [""])[0]
                with TRASH_LOCK:
                    # Keep lookup, archival and deletion in one critical
                    # section.  Otherwise two rapid clicks can archive the
                    # same item twice or let a second request overwrite the
                    # first trash update.
                    line = self.selected_line(item_id)
                    if line is None:
                        return self.reply_json({"error": "History entry not found"}, 404)
                    decoded = subprocess.run(["cliphist", "decode"], input=line + b"\n", capture_output=True, timeout=2)
                    if decoded.returncode:
                        raise RuntimeError(decoded.stderr.decode("utf-8", "replace"))
                    entry = {"id": uuid.uuid4().hex, "source_id": item_id,
                             "preview": line.split(b"\t", 1)[1].decode("utf-8", "replace"),
                             "content": decoded.stdout.decode("utf-8", "replace"), "deleted_at": time.time()}
                    entries = self.read_trash()
                    entries.insert(0, entry)
                    self.write_trash(entries)
                    result = subprocess.run(["cliphist", "delete"], input=line + b"\n", capture_output=True, timeout=2)
                    if result.returncode:
                        # Do not leave an apparently restorable item when the
                        # source was not actually deleted.
                        self.write_trash([item for item in entries if item.get("id") != entry["id"]])
                        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
                self.reply_json({"ok": True, "trash_id": entry["id"]})
            elif parsed.path == "/history/undo":
                with TRASH_LOCK:
                    entries = self.read_trash()
                    if not entries:
                        return self.reply_json({"error": "La papelera está vacía"}, 404)
                    entry = self.restore_entry(entries[0]["id"])
                self.reply_json({"ok": True, "preview": entry["preview"]})
            elif parsed.path == "/trash":
                with TRASH_LOCK:
                    entries = self.read_trash()
                self.reply_json([{key: item[key] for key in ("id", "source_id", "preview", "deleted_at") if key in item}
                                 for item in entries])
            elif parsed.path == "/trash/restore":
                entry = self.restore_entry(query.get("id", [""])[0])
                if entry is None:
                    return self.reply_json({"error": "Elemento no encontrado en la papelera"}, 404)
                self.reply_json({"ok": True, "preview": entry.get("preview", "")})
            elif parsed.path == "/trash/delete":
                item_id = query.get("id", [""])[0]
                with TRASH_LOCK:
                    entries = self.read_trash()
                    if not any(item.get("id") == item_id for item in entries):
                        return self.reply_json({"error": "Elemento no encontrado en la papelera"}, 404)
                    self.write_trash([item for item in entries if item.get("id") != item_id])
                self.reply_json({"ok": True})
            elif parsed.path == "/trash/wipe":
                with TRASH_LOCK:
                    self.write_trash([])
                self.reply_json({"ok": True})
            elif parsed.path == "/history/wipe":
                result = subprocess.run(["cliphist", "wipe"], capture_output=True, timeout=2)
                if result.returncode:
                    raise RuntimeError(result.stderr.decode("utf-8", "replace"))
                self.reply_json({"ok": True})
            elif parsed.path == "/sessions":
                self.handle_sessions(query)
            elif parsed.path == "/session":
                self.handle_session(query)
            elif parsed.path.startswith("/vendor/"):
                self.serve_vendor(parsed.path)
            else:
                with open(os.path.join(SCRIPT_DIR, "index-overlay.html"), "rb") as stream:
                    content = stream.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_cors_header()
                self.end_headers()
                self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404)
        except Exception as exc:
            self.reply_json({"error": str(exc)}, 500)

    def do_POST(self):
        """Handle ``/copy``, the only mutating endpoint."""
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/copy":
                length = int(self.headers.get("Content-Length", 0))
                content = self.rfile.read(length).decode("utf-8")
                self.copy_excluded(content)
                self.reply_json({"ok": True})
            else:
                self.send_error(404)
        except Exception as exc:
            self.reply_json({"error": str(exc)}, 500)

    def do_OPTIONS(self):
        """Answer CORS preflight, with headers only for allowed origins."""
        # Only the overlay's own origin may preflight; everything else is
        # answered without CORS headers so the browser blocks it.
        origin = self.cors_origin()
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *_args):
        """Silence per-request logging; the launcher already writes a log."""
        pass


class ReusableServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server that can rebind its port right after a restart."""
    allow_reuse_address = True


if __name__ == "__main__":
    ReusableServer(("127.0.0.1", PORT), Handler).serve_forever()
