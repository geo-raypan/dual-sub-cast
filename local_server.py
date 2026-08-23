"""Local media server with HTTP Range support, required for Chromecast seeking.

Usage:
    python local_server.py [port]

No files are copied anywhere. "Choose Video/Subtitle" in the sender page opens
a native file picker (running here on the server, so it can browse the whole
disk); the chosen absolute path is remembered in selection.json next to this
script, and /play streams directly from that path.

Run this on the same machine as Chrome. It binds to 0.0.0.0 so the Chromecast
device on your LAN can fetch the files directly.
"""
import http.server
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import urllib.parse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
APP_DIR = os.path.dirname(os.path.abspath(__file__))
RECEIVER_DIR = os.path.join(APP_DIR, "receiver")
SELECTION_FILE = os.path.join(APP_DIR, "selection.json")

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

FILE_TYPES = {
    "video": [("Video files", "*.mp4 *.mkv *.webm *.mov *.avi *.wmv"), ("All files", "*.*")],
    "sub1": [("Subtitle files", "*.srt *.vtt *.ass *.ssa *.sbv"), ("All files", "*.*")],
    "sub2": [("Subtitle files", "*.srt *.vtt *.ass *.ssa *.sbv"), ("All files", "*.*")],
}


def load_selection():
    try:
        with open(SELECTION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"video": None, "sub1": None, "sub2": None}


def save_selection(selection):
    with open(SELECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(selection, f)


SELECTION = load_selection()

SUB_ADJUST_FILE = os.path.join(APP_DIR, "sub_adjustments.json")


def load_sub_adjustments():
    try:
        with open(SUB_ADJUST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_sub_adjustments(adjustments):
    with open(SUB_ADJUST_FILE, "w", encoding="utf-8") as f:
        json.dump(adjustments, f)


# Keyed by absolute subtitle path -> {"offset": seconds, "speed": multiplier}.
# Lets a per-file delay/speed correction (tuned once in preview.html) get
# remembered and re-applied automatically next time that same file is picked.
SUB_ADJUSTMENTS = load_sub_adjustments()


def pick_file(kind):
    """Open a native file-picker dialog on the server machine and return the
    chosen absolute path, or None if the user cancelled."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(title=f"Choose {kind}", filetypes=FILE_TYPES.get(kind, [("All files", "*.*")]))
    root.destroy()
    return path or None


class RangeRequestHandler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/whoami":
            self.send_whoami()
            return
        if parsed.path == "/selection":
            self.send_json(200, SELECTION)
            return
        if parsed.path == "/pick":
            self.handle_pick(parsed)
            return
        if parsed.path == "/clear_selection":
            self.handle_clear_selection(parsed)
            return
        if parsed.path == "/play":
            self.handle_play(parsed)
            return
        if parsed.path == "/sub_adjustment":
            self.handle_get_sub_adjustment(parsed)
            return
        if parsed.path in ("/", "/index.html", "/sender.html", "/preview.html"):
            self.serve_app_file("index.html")
            return
        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/apply_style":
            self.handle_apply_style()
            return
        if parsed.path == "/sub_adjustment":
            self.handle_set_sub_adjustment()
            return
        self.send_error(404, "Not found")

    def handle_pick(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        kind = (query.get("kind") or [""])[0]
        if kind not in ("video", "sub1", "sub2"):
            self.send_json(400, {"ok": False, "error": "kind must be video, sub1, or sub2"})
            return

        path = pick_file(kind)
        if not path:
            self.send_json(200, {"ok": False, "error": "cancelled"})
            return

        SELECTION[kind] = path
        save_selection(SELECTION)
        self.send_json(200, {"ok": True, "path": path, "name": os.path.basename(path)})

    def handle_clear_selection(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        kind = (query.get("kind") or [""])[0]
        if kind not in ("video", "sub1", "sub2"):
            self.send_json(400, {"ok": False, "error": "kind must be video, sub1, or sub2"})
            return
        SELECTION[kind] = None
        save_selection(SELECTION)
        self.send_json(200, {"ok": True})

    def handle_play(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        which = (query.get("which") or [""])[0]
        path = SELECTION.get(which)
        if not path or not os.path.isfile(path):
            self.send_error(404, "No file selected for this role, or it no longer exists")
            return
        self.serve_with_range(path)

    def handle_get_sub_adjustment(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        path = (query.get("path") or [""])[0]
        adj = SUB_ADJUSTMENTS.get(path, {"offset": 0})
        self.send_json(200, {"offset": adj.get("offset", 0)})

    def handle_set_sub_adjustment(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = payload["path"]
            offset = float(payload["offset"])
            if not path:
                raise ValueError("invalid path")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            self.send_json(400, {"ok": False, "error": f"invalid payload: {e}"})
            return

        SUB_ADJUSTMENTS[path] = {"offset": offset}
        save_sub_adjustments(SUB_ADJUSTMENTS)
        self.send_json(200, {"ok": True})

    def handle_apply_style(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            font_size = float(payload["font_size"])
            sub1_bottom = float(payload["sub1_bottom"])
            sub2_bottom = float(payload["sub2_bottom"])
            for v in (font_size, sub1_bottom, sub2_bottom):
                if not (0 <= v <= 100):
                    raise ValueError("value out of range")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            self.send_json(400, {"ok": False, "error": f"invalid payload: {e}"})
            return

        index_path = os.path.join(RECEIVER_DIR, "index.html")
        try:
            html = open(index_path, "r", encoding="utf-8").read()
            html, n1 = re.subn(r"font-size: [\d.]+vh;", f"font-size: {font_size}vh;", html, count=1)
            html, n2 = re.subn(r"(#sub1 \{ bottom: )[\d.]+vh;", rf"\g<1>{sub1_bottom}vh;", html, count=1)
            html, n3 = re.subn(r"(#sub2 \{ bottom: )[\d.]+vh;", rf"\g<1>{sub2_bottom}vh;", html, count=1)
            if not (n1 and n2 and n3):
                raise RuntimeError("expected CSS patterns not found in receiver/index.html")
            open(index_path, "w", encoding="utf-8").write(html)

            subprocess.run(["git", "-C", RECEIVER_DIR, "add", "index.html"], check=True, capture_output=True)
            commit = subprocess.run(
                ["git", "-C", RECEIVER_DIR, "commit", "-m", "Tune subtitle style via preview"],
                capture_output=True, text=True
            )
            if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
                raise RuntimeError(commit.stdout + commit.stderr)
            subprocess.run(["git", "-C", RECEIVER_DIR, "push"], check=True, capture_output=True)
        except Exception as e:
            self.send_json(500, {"ok": False, "error": str(e)})
            return

        self.send_json(200, {"ok": True})

    def send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_app_file(self, name):
        path = os.path.join(APP_DIR, name)
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_whoami(self):
        body = json.dumps({"lan_ip": lan_ip(), "port": PORT}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_with_range(self, path):
        file_size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

        if not range_header:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with open(path, "rb") as f:
                self.copy_chunked(f, file_size)
            return

        match = RANGE_RE.match(range_header)
        if not match:
            self.send_error(416, "Invalid Range header")
            return

        start_str, end_str = match.groups()
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with open(path, "rb") as f:
            f.seek(start)
            self.copy_chunked(f, length)

    def copy_chunked(self, f, remaining, chunk_size=64 * 1024):
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                break
            self.wfile.write(chunk)
            remaining -= len(chunk)


def lan_ip():
    # Connect toward a LAN address (the Chromecast itself) rather than the
    # public internet, so a VPN's default route doesn't hijack this and hand
    # back the VPN tunnel's IP instead of the real LAN-facing IP.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.50.117", 8009))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    ip = lan_ip()
    print(f"Local:  http://127.0.0.1:{PORT}/")
    print(f"LAN (use this from the sender page / extension): http://{ip}:{PORT}/")
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), RangeRequestHandler).serve_forever()
