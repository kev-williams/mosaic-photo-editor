#!/usr/bin/env python3
"""Start Mosaic Photo-Editor in its own window.

What it does:
  1. Starts the app server (Streamlit) on this computer only (127.0.0.1, a free port).
  2. Opens a native macOS window (pywebview / WebKit) showing it. The Dock icon, Cmd+Q and
     closing the window all quit the app; the server stops with it and all memory is freed.
  3. Gives the app native folder pickers (as a sheet on the window) through a small local
     bridge protected by a random key.

Options:
  --browser   open in your default browser instead of a window. The app then quits by itself
              5 minutes after the last browser tab is closed, or with Quit in the sidebar.
  --port N    preferred port (default 8501; the next free one is used if it is taken).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mosaic import APP_NAME, __version__  # noqa: E402
from mosaic.paths import LOG_DIR, ensure  # noqa: E402

ICON = ROOT / "assets" / "AppIcon.icns"
LOG_FILE = LOG_DIR / "mosaic-photo-editor.log"
LOG_MAX = 5 * 1024 * 1024
IDLE_EXIT_S = 300

def _icon_svg() -> str:
    try:
        return (ROOT / "assets" / "icon.svg").read_text()
    except OSError:
        return ""


# Shown in the window while the app starts. The status line is updated by boot() as it goes.
LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center; overflow:hidden;
  font:15px -apple-system, BlinkMacSystemFont, sans-serif; color:#5A2A1E; background:#FFF5EF; }
.b { text-align:center; width:340px; }
.i { width:96px; height:96px; margin:0 auto 18px; animation:p 1.8s ease-in-out infinite; }
.i svg { width:100%; height:100%; filter:drop-shadow(0 8px 18px rgba(142,36,48,.25)); }
.t { font-size:24px; font-weight:700; color:#C8343E; letter-spacing:-.01em; }
.t span { font-weight:400; color:#5A2A1E; }
.m { margin-top:6px; min-height:20px; color:#8E6A60; }
.bar { margin:22px auto 0; width:220px; height:4px; border-radius:2px; background:#F2D4C4; overflow:hidden; }
.bar::after { content:""; display:block; width:40%; height:100%; border-radius:2px;
  background:linear-gradient(90deg,#B8263A,#E06A35); animation:s 1.2s ease-in-out infinite; }
@keyframes s { 0% { transform:translateX(-100%); } 100% { transform:translateX(250%); } }
@keyframes p { 0%,100% { transform:scale(1); } 50% { transform:scale(1.04); } }
</style></head><body><div class="b"><div class="i">ICON</div>
<div class="t">Mosaic <span>Photo-Editor</span></div><div class="m" id="msg">Starting...</div>
<div class="bar" id="bar"></div></div></body></html>""".replace("ICON", _icon_svg())


def log(msg: str):
    try:
        with open(LOG_FILE, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} launcher: {msg}\n")
    except OSError:
        pass


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port")


def start_server(port: int, extra_env: dict) -> subprocess.Popen:
    ensure()
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX:
        LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", **extra_env}
    out = open(LOG_FILE, "a")
    cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
           "--server.port", str(port), "--server.address", "127.0.0.1", "--server.headless", "true",
           "--browser.gatherUsageStats", "false"]
    log(f"starting {APP_NAME} {__version__} on port {port}")
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 120) -> bool:
    url = f"http://127.0.0.1:{port}/_stcore/health"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def stop_server(proc: subprocess.Popen | None):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    log("server stopped")


# --------------------------------------------------------------------------- native bridge
class Bridge:
    """Tiny local HTTP service the app calls for things only the window can do."""

    def __init__(self):
        self.token = secrets.token_hex(24)
        self.window = None
        self.on_quit = None
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.client_address[0] != "127.0.0.1" or not secrets.compare_digest(
                        self.headers.get("X-Mosaic-Token", ""), bridge.token):
                    return self._reply(403, {"error": "forbidden"})
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                try:
                    if url.path == "/pick-folder":
                        return self._reply(200, {"paths": bridge.dialog("folder", q.get("start", ""))})
                    if url.path == "/pick-files":
                        return self._reply(200, {"paths": bridge.dialog("files", q.get("start", ""))})
                    if url.path == "/browse":
                        return self._reply(200, {"paths": bridge.browse(q.get("start", ""))})
                    if url.path == "/quit":
                        self._reply(200, {"ok": True})
                        threading.Timer(0.3, bridge.quit).start()
                        return None
                except Exception as e:
                    return self._reply(500, {"error": str(e)})
                return self._reply(404, {"error": "not found"})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def dialog(self, kind: str, start: str):
        import webview
        if self.window is None:
            return []
        folder = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        opener = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        start = start if start and os.path.isdir(start) else str(Path.home() / "Pictures")
        if kind == "folder":
            res = self.window.create_file_dialog(folder, directory=start)
        else:
            res = self.window.create_file_dialog(
                opener, directory=start, allow_multiple=True,
                file_types=("Images (*.jpg;*.jpeg;*.png;*.tif;*.tiff;*.heic;*.heif;*.webp;*.avif;*.bmp;*.gif)",))
        return [str(p) for p in (res or [])]

    def browse(self, start: str):
        """macOS open panel that accepts photos and folders together (pywebview's dialogs can't)."""
        start = start if start and os.path.isdir(start) else str(Path.home() / "Pictures")
        try:
            import AppKit
            import Foundation
            from PyObjCTools import AppHelper
        except ImportError:
            return self.dialog("files", start)
        result: dict = {}
        done = threading.Event()

        def run():
            try:
                panel = AppKit.NSOpenPanel.openPanel()
                panel.setCanChooseFiles_(True)
                panel.setCanChooseDirectories_(True)
                panel.setAllowsMultipleSelection_(True)
                panel.setCanCreateDirectories_(False)
                panel.setPrompt_("Open")
                panel.setMessage_("Choose photos, or a folder of photos")
                panel.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(start))
                try:
                    import UniformTypeIdentifiers as uti
                    panel.setAllowedContentTypes_([uti.UTType.typeWithIdentifier_("public.image"),
                                                   uti.UTType.typeWithIdentifier_("public.folder")])
                except Exception:
                    pass
                AppKit.NSApp.activateIgnoringOtherApps_(True)
                if panel.runModal() == AppKit.NSModalResponseOK:
                    result["paths"] = [str(u.path()) for u in panel.URLs()]
            except Exception as e:
                log(f"browse failed: {e}")
            finally:
                done.set()

        AppHelper.callAfter(run)
        done.wait()
        return result.get("paths", [])

    def quit(self):
        if self.on_quit:
            self.on_quit()


# --------------------------------------------------------------------------- modes
def _mac_app_name():
    """Show the app's name (not 'Python') in the menu bar."""
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = APP_NAME
    except Exception:
        pass


def run_window(port: int) -> int:
    import webview
    bridge = Bridge()
    proc_box: dict = {}
    window = webview.create_window(APP_NAME, html=LOADING_HTML, width=1440, height=920, min_size=(1080, 700),
                                   background_color="#FFF5EF", text_select=True, zoomable=True)
    bridge.window = window
    bridge.on_quit = window.destroy

    def status(text: str):
        try:
            window.evaluate_js(f"document.getElementById('msg').textContent = {json.dumps(text)}")
        except Exception:
            pass

    def boot():
        t0 = time.time()
        proc = start_server(port, {"MOSAIC_LAUNCHER_URL": bridge.url, "MOSAIC_LAUNCHER_TOKEN": bridge.token})
        proc_box["proc"] = proc
        status("Starting the photo engine...")
        if not wait_ready(port, proc):
            window.load_html(LOADING_HTML.replace("Starting...", f"Could not start. See the log: {LOG_FILE}")
                             .replace('<div class="bar" id="bar"></div>', ""))
            return
        status("Opening...")
        log(f"server ready in {time.time() - t0:.1f} s")
        try:
            from mosaic import timing
            timing.log("app start", time.time() - t0, detail="window")
        except Exception:
            pass
        window.load_url(f"http://127.0.0.1:{port}/")
        proc.wait()                                    # server stopped (e.g. crashed): close the window
        log(f"server exited with {proc.returncode}")
        window.destroy()

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    _mac_app_name()
    try:
        webview.start(boot, icon=str(ICON) if ICON.exists() else None, private_mode=True)
    finally:
        stop_server(proc_box.get("proc"))
    return 0


def run_browser(port: int) -> int:
    proc = start_server(port, {"MOSAIC_IDLE_EXIT": str(IDLE_EXIT_S)})
    if not wait_ready(port, proc):
        print(f"{APP_NAME} could not start. See {LOG_FILE}", file=sys.stderr)
        stop_server(proc)
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} is running at {url}  (quits {IDLE_EXIT_S // 60} minutes after the last tab closes)")
    webbrowser.open(url)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        stop_server(proc)


def main() -> int:
    ap = argparse.ArgumentParser(description=f"Start {APP_NAME}")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MOSAIC_PORT", 8501)))
    args = ap.parse_args()
    for sig in (signal.SIGTERM, signal.SIGHUP):          # stop the server too when asked to quit
        signal.signal(sig, lambda *_: sys.exit(0))
    port = free_port(args.port)
    if not args.browser:
        try:
            import webview  # noqa: F401
        except Exception as e:
            log(f"window not available ({e}); using the browser")
            args.browser = True
    return run_browser(port) if args.browser else run_window(port)


if __name__ == "__main__":
    sys.exit(main())
