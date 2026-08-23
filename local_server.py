"""Local media server with HTTP Range support, required for Chromecast seeking.

Usage:
    python local_server.py [media_dir] [port]

Put your video file and two subtitle files (.vtt) into media_dir (default: ./media).
The server also exposes GET /list.json with the file listing, used by the
Chrome extension popup to build the picker.

Run this on the same machine as Chrome. It binds to 0.0.0.0 so the Chromecast
device on your LAN can fetch the files directly.
"""
import glob
import http.server
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse

MEDIA_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "media")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8787
APP_DIR = os.path.dirname(os.path.abspath(__file__))
RECEIVER_DIR = os.path.join(APP_DIR, "receiver")

os.makedirs(MEDIA_DIR, exist_ok=True)

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

# Codecs Chrome's <video> tag (and the CAF cast-media-player) can't play.
# Anything in here, or any video taller than 1080p, gets auto-transcoded on upload.
INCOMPATIBLE_VIDEO_CODECS = {"hevc", "h265", "av1", "mpeg2video", "mpeg4", "wmv3", "vc1"}
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv")

# name -> {"status": "checking"|"transcoding"|"done"|"error", "output": str|None, "error": str|None}
TRANSCODE_STATUS = {}
TRANSCODE_LOCK = threading.Lock()


def find_tool(name):
    found = shutil.which(name)
    if found:
        return found
    # Common manual-install locations on Windows when it's not on PATH.
    for pattern in (
        rf"C:\ffmpeg\**\{name}.exe",
        rf"C:\Program Files\ffmpeg\**\{name}.exe",
        rf"C:\Program Files (x86)\ffmpeg\**\{name}.exe",
    ):
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    return None


FFMPEG_PATH = find_tool("ffmpeg")
FFPROBE_PATH = find_tool("ffprobe")


def probe_video(path):
    """Return (codec_name, height) for the first video stream, or (None, None) if ffprobe is unavailable/fails."""
    if not FFPROBE_PATH:
        return None, None
    try:
        out = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,height", "-of", "json", path],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(out.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        return stream.get("codec_name"), stream.get("height")
    except (subprocess.SubprocessError, json.JSONDecodeError, IndexError, OSError):
        return None, None


def run_ffmpeg(args, partial_output, final_output):
    subprocess.run([FFMPEG_PATH, "-y"] + args + [partial_output], capture_output=True, text=True, check=True)
    os.replace(partial_output, final_output)


def transcode_if_needed(name):
    with TRANSCODE_LOCK:
        TRANSCODE_STATUS[name] = {"status": "checking", "output": None, "error": None}

    path = os.path.join(MEDIA_DIR, name)
    codec, height = probe_video(path)
    needs_transcode = codec in INCOMPATIBLE_VIDEO_CODECS or (isinstance(height, int) and height > 1080)
    ext = os.path.splitext(name)[1].lower()
    stem = os.path.splitext(name)[0]

    if not needs_transcode:
        # Codec/resolution are already fine. But MP4/MOV files muxed with the
        # index (moov atom) at the end of the file -- common in scene-release
        # remuxes -- will play a few seconds then stall for progressive HTTP
        # streaming. A lossless remux with faststart fixes this without
        # re-encoding, so do it as a fast, cheap safety pass for those.
        if ext in (".mp4", ".mov") and FFMPEG_PATH:
            with TRANSCODE_LOCK:
                TRANSCODE_STATUS[name] = {"status": "transcoding", "output": None, "error": None}
            final_output = os.path.join(MEDIA_DIR, f"{stem}.faststart.mp4")
            partial_output = final_output + ".part"
            try:
                run_ffmpeg(["-i", path, "-c", "copy", "-movflags", "+faststart"], partial_output, final_output)
                with TRANSCODE_LOCK:
                    TRANSCODE_STATUS[name] = {"status": "done", "output": os.path.basename(final_output), "error": None}
            except subprocess.CalledProcessError as e:
                if os.path.exists(partial_output):
                    os.remove(partial_output)
                # Faststart remux failing isn't fatal -- fall back to the original file as-is.
                with TRANSCODE_LOCK:
                    TRANSCODE_STATUS[name] = {"status": "done", "output": name, "error": None}
        else:
            with TRANSCODE_LOCK:
                TRANSCODE_STATUS[name] = {"status": "done", "output": name, "error": None}
        return

    if not FFMPEG_PATH:
        with TRANSCODE_LOCK:
            TRANSCODE_STATUS[name] = {
                "status": "error", "output": None,
                "error": f"'{name}' needs transcoding (codec={codec}, height={height}) but ffmpeg wasn't found on PATH or in C:\\ffmpeg."
            }
        return

    with TRANSCODE_LOCK:
        TRANSCODE_STATUS[name] = {"status": "transcoding", "output": None, "error": None}

    final_output = os.path.join(MEDIA_DIR, f"{stem}.1080p.mp4")
    partial_output = final_output + ".part"
    try:
        run_ffmpeg(
            ["-i", path, "-vf", "scale=-2:min(1080\\,ih)",
             "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-c:a", "copy", "-movflags", "+faststart"],
            partial_output, final_output
        )
        with TRANSCODE_LOCK:
            TRANSCODE_STATUS[name] = {"status": "done", "output": os.path.basename(final_output), "error": None}
    except subprocess.CalledProcessError as e:
        if os.path.exists(partial_output):
            os.remove(partial_output)
        with TRANSCODE_LOCK:
            TRANSCODE_STATUS[name] = {"status": "error", "output": None, "error": e.stderr[-500:] if e.stderr else str(e)}


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=MEDIA_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/list.json":
            self.send_list_json()
            return
        if parsed.path == "/whoami":
            self.send_whoami()
            return
        if parsed.path == "/transcode_status":
            self.send_transcode_status(parsed)
            return
        if parsed.path in ("/", "/sender.html"):
            self.serve_app_file("sender.html")
            return
        if parsed.path == "/preview.html":
            self.serve_app_file("preview.html")
            return
        self.serve_with_range()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/apply_style":
            self.handle_apply_style()
            return
        if parsed.path == "/upload":
            self.handle_upload(parsed)
            return
        self.send_error(404, "Not found")

    def handle_upload(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        names = query.get("name")
        if not names or not names[0]:
            self.send_json(400, {"ok": False, "error": "missing ?name= query param"})
            return
        # basename() strips any directory components so the upload can't escape MEDIA_DIR
        filename = os.path.basename(names[0])
        if not filename:
            self.send_json(400, {"ok": False, "error": "invalid filename"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        dest_path = os.path.join(MEDIA_DIR, filename)
        try:
            with open(dest_path, "wb") as f:
                remaining = length
                chunk_size = 64 * 1024
                while remaining > 0:
                    chunk = self.rfile.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self.send_json(500, {"ok": False, "error": str(e)})
            return

        if filename.lower().endswith(VIDEO_EXTENSIONS):
            threading.Thread(target=transcode_if_needed, args=(filename,), daemon=True).start()
        self.send_json(200, {"ok": True, "name": filename})

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

    def send_transcode_status(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        name = (query.get("name") or [""])[0]
        with TRANSCODE_LOCK:
            status = TRANSCODE_STATUS.get(name, {"status": "unknown", "output": None, "error": None})
        self.send_json(200, status)

    def send_whoami(self):
        body = json.dumps({"lan_ip": lan_ip(), "port": PORT}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_list_json(self):
        files = sorted(
            f for f in os.listdir(MEDIA_DIR)
            if os.path.isfile(os.path.join(MEDIA_DIR, f)) and not f.endswith(".part")
        )
        body = json.dumps({"files": files}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_with_range(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return

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
    print(f"Serving {MEDIA_DIR}")
    print(f"Local:  http://127.0.0.1:{PORT}/")
    print(f"LAN (use this from the sender page / extension): http://{ip}:{PORT}/")
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), RangeRequestHandler).serve_forever()
