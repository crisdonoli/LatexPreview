#!/usr/bin/env python3
"""Guard the cliphist watcher against LaTeX-overlay copies.

``wl-paste --watch`` pipes each new clipboard selection to its command on
stdin.  This script forwards those bytes to ``cliphist store``, except when
the LaTeX overlay server just placed the exact same content on the clipboard
through ``POST /copy``.  The server records a short-lived, content-hashed
marker *before* running wl-copy; when the watcher fires with matching bytes we
consume the marker and drop the entry.

Design notes:
  * Fail open: no marker, a stale marker or a hash mismatch always stores the
    content, so ordinary copies are never lost.
  * The marker is only removed on a match or once expired, so a watcher event
    for a different clipboard selection can never eat the overlay's marker.
  * State lives beside the server state under STATE_DIR (XDG_STATE_HOME).
  * The copied bytes never change: we hash and forward exactly what wl-paste
    handed us.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "latex-overlay",
)
SUPPRESS_FILE = os.path.join(STATE_DIR, "suppress.json")
TTL_SECONDS = 2.0


def remove_marker():
    """Delete the marker file; an already missing one is not an error."""
    try:
        os.unlink(SUPPRESS_FILE)
    except OSError:
        pass


def is_suppressed(data, now=None):
    """True when ``data`` matches a fresh overlay-copy marker; consume it."""
    now = time.time() if now is None else now
    try:
        with open(SUPPRESS_FILE, encoding="utf-8") as stream:
            marker = json.load(stream)
    except (OSError, ValueError):
        return False
    try:
        fresh = (now - float(marker.get("at", 0))) <= TTL_SECONDS
    except (TypeError, ValueError):
        fresh = False
    if not fresh:
        remove_marker()
        return False
    if marker.get("sha256") != hashlib.sha256(data).hexdigest():
        return False
    remove_marker()
    return True


def store(data):
    """Pipe ``data`` into ``cliphist store`` and return its exit status."""
    return subprocess.run(["cliphist", "store"], input=data).returncode


def main():
    """Read the watcher payload from stdin and store it unless suppressed."""
    data = sys.stdin.buffer.read()
    if is_suppressed(data):
        return 0
    return store(data)


if __name__ == "__main__":
    sys.exit(main())
