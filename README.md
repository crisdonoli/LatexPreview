# LaTeX Overlay
[![OpenCode](https://img.shields.io/badge/OpenCode-Compatible-0052CC?style=flat-square&logo=code&logoColor=white)](https://github.com/) [![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![Bash](https://img.shields.io/badge/Bash-4.4%2B-4EAA25?style=flat-square&logo=gnu-bash&logoColor=white)](https://www.gnu.org/software/bash/) [![NixOS](https://img.shields.io/badge/NixOS-26.05-5277C3?style=flat-square&logo=nixos&logoColor=white)](https://nixos.org/)

A floating LaTeX preview for Wayland/Hyprland. Anything you copy is rendered
with KaTeX in a transparent, always-on-top overlay strip (or in a small popup
window), so agent output, notes and formulas can be read without leaving the
window you are working in.

Two front ends share the same renderer: **`latex-overlay`** (clipboard watcher,
session browser, history/trash sidebar) and **`latex-preview`** (standalone
type-and-render popup, no server needed).

## Features

- Live clipboard preview: the overlay polls the Wayland clipboard and renders
  the LaTeX it finds (`$…$`, `$$…$$`, `\[…\]`, `\(…\)`, `\begin{env}…\end{env}`).
- Read/edit toggle (Ctrl+E) with a raw-source textarea.
- Clipboard history browser backed by `cliphist`, with a soft-delete trash
  (delete → trash → restore / delete permanently / wipe).
- OpenCode session browser: lists local sessions with open/busy badges, shows
  the conversation transcript, follows the newest message, optional subagents.
- Copy button routed through the server so overlay copies are excluded from the
  `cliphist` history instead of polluting it.
- Zoom 50 %–200 %, message/clipboard stepping from the toolbar or keyboard.
- Wayland layer-shell overlay (Gtk4LayerShell) with a Chromium `--app` fallback.
- Standalone popup with KaTeX, Mermaid diagrams and ASCII-art block rendering.

## Architecture

| Component | Role | Talks to |
|---|---|---|
| `latex-overlay` (bash) | starts/stops the GTK app, writes PID file, floats/pins/centers the window via `hyprctl` | `latex-overlay-gtk.py` |
| `latex-overlay-gtk.py` | imports the server module, runs it on a daemon thread, opens the transparent layer-shell window with WebKit6 | server module via `importlib`, WebView over loopback HTTP |
| `latex-overlay-server.py` | `ThreadingHTTPServer` on `127.0.0.1:17832`: serves the UI + `/vendor`, clipboard and session API | `cliphist`, `wl-copy`, `wl-paste`, `opencode.db`, state dir |
| `index-overlay.html` | overlay UI: Live/Edit mode, zoom, sessions/history/trash sidebar | server JSON/text endpoints, polls `/clipboard` every 300 ms |
| `cliphist-guard.py` | wrapper for `wl-paste --watch`: forwards to `cliphist store` unless the copy came from the overlay | `suppress.json` written by the server |
| `latex-preview` (bash) | standalone popup launcher, Chromium → Firefox → Python+WebKit fallback | `index.html` |
| `index.html` | self-contained preview page (no server) | nothing |

```
wl-paste --watch ──► cliphist-guard.py ──► cliphist store ──┐
                                                            │
overlay UI ──HTTP──► server ──cliphist list/decode/delete───┘
   │                   │
   │                   ├──wl-copy (POST /copy, writes suppress.json first)
   │                   ├──sqlite (ro) opencode.db ◄── latex-overlay-sessions.js plugin
   │                   └──$XDG_STATE_HOME/latex-overlay/{trash,suppress,session-history}.json
   └──WebKit/Chromium
```

## Requirements

- Python 3.11+ (server and guard use only the standard library).
- PyGObject with Gtk 4, WebKit 6 (WebKitGTK 6.0) for the native overlay —
  `pip install -r requirements.txt` is optional; a distro/nix package works too.
- Gtk4LayerShell (optional; without it the window is a normal floating window).
- `cliphist` and `wl-clipboard` (`wl-copy`, `wl-paste`) on `PATH`.
- Vendored KaTeX in `vendor/katex/` (already in the tree; the overlay also
  falls back to the jsDelivr CDN if the local copy is missing).
- A Chromium-family browser for `--browser` mode and for `latex-preview`;
  `index.html` additionally loads Mermaid from jsDelivr.

## Installation / Usage

No build step. Clone the directory and run the launchers.

```sh
./latex-overlay              # start the overlay (GTK + layer shell)
./latex-overlay --browser    # start with a Chromium --app window instead of GTK
./latex-overlay status       # Running (PID: …) | Not running
./latex-overlay stop         # kill the PID and free port 17832

./latex-preview                  # standalone popup, empty input
./latex-preview "\frac{a}{b}"    # standalone popup pre-filled from the URL hash
```

`latex-overlay` runs the GUI in the background, logs to
`$XDG_STATE_HOME/latex-overlay/launch.log` (truncated to the last 64 KiB on each
start), and then asks Hyprland to float, resize (800×300), center and pin the
window. If the process dies within 2 s the tail of the log is printed and a
`notify-send` critical notification is attempted.

### Clipboard guard

Point the watcher at the guard instead of at `cliphist store` directly:

```sh
exec wl-paste --watch /path/to/cliphist-guard.py
```

The guard reads the payload from stdin, hashes it, and stores it through
`cliphist store` unless a matching fresh marker exists. The marker is written by
the server before `wl-copy`, so plain `POST /copy` copies never enter the
clipboard history. Without the guard installed, overlay copies are stored like
any other copy.

### OpenCode session registry

The "Sessions" tab needs two things: the local OpenCode database and a registry
of live processes. The registry files (`proc-*.json` under
`$XDG_RUNTIME_DIR/latex-overlay/sessions`, or `$XDG_STATE_HOME/latex-overlay/sessions`
when `XDG_RUNTIME_DIR` is unset) are written by the OpenCode plugin
`latex-overlay-sessions.js`, which lives with the OpenCode config and is not part
of this directory. Each file carries `pid`, `tty`, `active_session`, `updated_at`
and a `sessions` map; the server prunes files whose pid is gone or whose
`updated_at` is older than 24 h. Without the plugin the transcript endpoints
still work, but no session shows an open/busy badge.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `XDG_STATE_HOME` | `~/.local/state` | root of `latex-overlay/` (trash.json, suppress.json, session-history.json, launch.log) |
| `XDG_RUNTIME_DIR` | unset | when set, the plugin registry is read from `$XDG_RUNTIME_DIR/latex-overlay/sessions` |
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | SQLite database opened read-only |

Constants in `latex-overlay-server.py`:

| Constant | Value | Meaning |
|---|---|---|
| `PORT` | `17832` | loopback HTTP port |
| `SUPPRESS_TTL` | `2.0 s` | lifetime of the cliphist suppression marker |
| `REGISTRY_TTL` | `24 h` | age after which a registry file is pruned |
| `SESSION_HISTORY_MAX` | `50` | remembered "previously opened" sessions |
| `MAX_SESSION_MESSAGES` | `50` | messages returned per transcript |
| `MAX_SESSION_BYTES` | `300_000` | byte budget per transcript |
| `SESSION_QUERY_MULTIPLIER` | `4` | over-fetch factor so tool-only messages don't starve the result |
| `DEFAULT_SESSIONS_LIMIT` / `MAX_SESSIONS_LIMIT` | `100` / `1000` | `/sessions?limit=` bounds |

`cliphist-guard.py` keeps its own `TTL_SECONDS = 2.0`; it must stay in sync with
`SUPPRESS_TTL`, since both sides compare against the same marker timestamp.

## HTTP API

Bound to `127.0.0.1:17832`. All endpoints except `POST /copy` are `GET`.

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | `index-overlay.html` |
| `/vendor/*` | GET | static files under `vendor/`, 404 outside that root |
| `/clipboard` | GET | current selection as plain text (`wl-paste --no-newline`) |
| `/history` | GET | `[{id, preview}]` from `cliphist list` |
| `/history/get?id=` | GET | full decoded entry, 404 when the id is gone |
| `/history/delete?id=` | GET | archive the entry in trash, then `cliphist delete` |
| `/history/undo` | GET | restore the newest trash entry to the clipboard |
| `/history/wipe` | GET | `cliphist wipe` (trash is untouched) |
| `/trash` | GET | `[{id, source_id, preview, deleted_at}]` |
| `/trash/restore?id=` | GET | copy the entry back and remove it from trash |
| `/trash/delete?id=` | GET | remove the entry permanently |
| `/trash/wipe` | GET | empty the trash |
| `/sessions?subagents=1&limit=N` | GET | DB sessions merged with live registry state |
| `/session?id=` | GET | `{title, agent, directory, messages[], truncated}` |
| `/copy` | POST | text/plain body; writes the suppress marker, then `wl-copy` |

Errors are JSON `{"error": "…"}` with 400/404/500; the handler never logs
per-request lines (the launcher log covers startup).

## Keyboard shortcuts

Overlay (`index-overlay.html`):

| Keys | Action |
|---|---|
| `Esc` | close the window |
| `Ctrl+Enter` | copy the current LaTeX |
| `Ctrl+E` | toggle Live/Edit mode |
| `Ctrl+D` | delete the selected clipboard entry (to trash) |
| `Ctrl+Z` | undo the last deletion |
| `Ctrl+←` / `Ctrl+→` | older / newer clipboard entry |
| `Alt+←` / `Alt+→` | older / newer message of the selected session |
| `Ctrl+=` / `Ctrl+-` / `Ctrl+0` | zoom in / out / reset (ignored while typing) |

Popup (`index.html`):

| Keys | Action |
|---|---|
| `Esc` | close, else clear the input |
| `Ctrl+Enter` | copy the LaTeX |
| `Tab` | insert two spaces in the textarea |

## File layout

```
latex-preview/
├── latex-overlay              # launcher/daemon wrapper (start, stop, status)
├── latex-preview              # standalone popup launcher
├── latex-overlay-gtk.py       # GTK4 + WebKit6 shell, --browser fallback
├── latex-overlay-server.py    # HTTP API, clipboard bridge, OpenCode reader
├── cliphist-guard.py          # wl-paste --watch → cliphist store guard
├── index-overlay.html         # overlay UI (served by the server)
├── index.html                 # standalone preview page (file://)
├── README.md
└── vendor/katex/              # katex.min.js, katex.min.css, fonts/
```

## Security notes

- The server binds `127.0.0.1` only; there is no remote listener.
- CORS is limited to `http://127.0.0.1:17832` and `http://localhost:17832`;
  other origins get no `Access-Control-Allow-Origin` header, so browsers block
  the JSON/text responses.
- `opencode.db` is opened with `mode=ro` plus `PRAGMA query_only=ON`; the
  overlay never writes to OpenCode state.
- Writes stay inside `$XDG_STATE_HOME/latex-overlay/` (`trash.json`,
  `suppress.json`, `session-history.json`, `launch.log`), all created with mode
  `0700` directories and atomic tmp+fsync+rename replacement.
- `/vendor/*` resolves the real path and refuses anything outside `vendor/`.
- Mutating endpoints use `GET`, so they inherit the browser's origin policy
  rather than CSRF tokens; loopback binding plus the CORS allow-list is the
  only boundary. Do not expose port 17832 beyond the host.

## Troubleshooting

**`latex-overlay: no browser found for --browser`** — no Chromium-family binary
on `PATH` (`brave`, `google-chrome-stable`, `google-chrome`, `chromium-browser`,
`chromium`, `microsoft-edge-stable`, `microsoft-edge`, `vivaldi-stable`,
`vivaldi`). Install one, or drop `--browser` and use the GTK path.

**`latex-overlay server did not answer at http://127.0.0.1:17832/`** — the port
is occupied or startup crashed. Run `./latex-overlay stop` (it also does
`fuser -k 17832/tcp`), then check `$XDG_STATE_HOME/latex-overlay/launch.log`.

**Clipboard preview never updates** — the overlay reads through `wl-paste`; check
that `wl-clipboard` works in the same session (`wl-paste --no-newline`). The
overlay ignores copies with no LaTeX-looking content (`$`, `$$`, `\[`, `\(`,
`\begin{`, `\section{`, `##` headings).

**Copied LaTeX does not appear in `cliphist`** — that is the guard working.
Overlay copies are suppressed on purpose; to keep them, remove
`cliphist-guard.py` from the `wl-paste --watch` command. Conversely, if overlay
copies flood the history, the guard is not installed or `XDG_STATE_HOME` differs
between the server and the guard.
