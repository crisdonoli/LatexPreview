#!/usr/bin/env python3
"""GTK4/WebKit6 shell for the LaTeX overlay.

Starts ``latex-overlay-server.py`` on a daemon thread, then shows a transparent
window anchored to the top edge through Gtk4LayerShell (a plain window when the
library or the compositor support is missing) pointed at
``http://127.0.0.1:17832/``. ``--browser`` skips Gtk and execs a
Chromium-family browser in ``--app`` mode instead.

I/O: loopback HTTP only; loads the overlay page into the WebView.

Side effects: ``--browser`` creates a profile directory under ``/tmp`` and
replaces the process via ``os.execv``.
"""
import importlib.util
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, GLib, Gtk, WebKit

SCRIPT_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
URL = f"http://{HOST}:17832/"
BROWSERS = (
    "brave",
    "google-chrome-stable",
    "google-chrome",
    "chromium-browser",
    "chromium",
    "microsoft-edge-stable",
    "microsoft-edge",
    "vivaldi-stable",
    "vivaldi",
)


def load_server():
    """Import ``latex-overlay-server.py`` as a module (the name is not importable)."""
    spec = importlib.util.spec_from_file_location(
        "latex_overlay_server", SCRIPT_DIR / "latex-overlay-server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_server(module):
    """Run the overlay server on a daemon thread and return it."""
    server = module.ReusableServer((HOST, module.PORT), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_ready(timeout=3.0):
    """Poll the overlay URL until it answers; raise once ``timeout`` expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urlopen(URL, timeout=0.5).read(1)
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"latex-overlay server did not answer at {URL}")


def launch_browser():
    """exec the first available Chromium-family browser as an app window."""
    profile = Path("/tmp/latex-overlay-brave-profile")
    profile.mkdir(parents=True, exist_ok=True)
    for name in BROWSERS:
        binary = shutil.which(name)
        if binary:
            os.execv(binary, [
                binary,
                f"--app={URL}",
                "--new-window",
                "--window-size=800,300",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--class=latex-overlay",
            ])
    raise SystemExit("latex-overlay: no browser found for --browser fallback")


def enable_layer_shell(window):
    """Anchor the window to the top edge; False when layer shell is unusable."""
    try:
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as layer_shell
    except (ImportError, ValueError):
        return False
    if not layer_shell.is_supported():
        return False
    layer_shell.init_for_window(window)
    layer_shell.set_namespace(window, "latex-overlay")
    layer_shell.set_layer(window, layer_shell.Layer.OVERLAY)
    layer_shell.set_keyboard_mode(window, layer_shell.KeyboardMode.ON_DEMAND)
    layer_shell.set_anchor(window, layer_shell.Edge.TOP, True)
    layer_shell.set_margin(window, layer_shell.Edge.TOP, 80)
    return True


def build_window():
    """Create the transparent window, its settings and the WebKit view."""
    settings = WebKit.Settings()
    settings.set_enable_webaudio(False)
    settings.set_allow_file_access_from_file_urls(False)
    settings.set_enable_developer_extras(False)

    session = WebKit.NetworkSession.new_ephemeral()
    webview = WebKit.WebView(settings=settings, network_session=session)
    webview.set_background_color(Gdk.RGBA(0, 0, 0, 0))
    webview.set_zoom_level(1.0)

    window = Gtk.Window(title="latex-overlay")
    window.set_default_size(800, 300)
    window.set_child(webview)
    return window, webview


def on_key_pressed(_controller, keyval, _keycode, _state, window, loop):
    """Hide the window on Escape and quit the loop once it is unmapped."""
    if keyval != Gdk.KEY_Escape:
        return False
    window.set_visible(False)
    GLib.timeout_add(120, loop.quit)
    return True


def main():
    """Honour ``--browser``, start the server, then run the GTK main loop."""
    if "--browser" in sys.argv[1:]:
        launch_browser()

    start_server(load_server())
    wait_ready()

    GLib.set_prgname("latex-overlay")
    Gtk.init()

    provider = Gtk.CssProvider()
    provider.load_from_data("window { background: transparent; }")
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    window, webview = build_window()
    enable_layer_shell(window)

    loop = GLib.MainLoop()
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", on_key_pressed, window, loop)
    window.add_controller(keys)

    webview.load_uri(URL)
    window.present()
    loop.run()


if __name__ == "__main__":
    main()
