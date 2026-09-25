#!/usr/bin/env python3
"""Serve media/ with video content types and HTTP byte ranges.

    python3 serve_media.py [port]      (default 8765)

Bram's loopback serves project files but has no video content types and
no Range support, so a browser would download an MP4 instead of playing
it (and couldn't seek). This fills that gap until Bram does it itself.

GET /sources lists the movies in sources/ and /sources/<name> streams one;
GET /record/status reports a running take's phase; POST /record {source}
starts record.sh (one take of that movie) for the app's Record button,
POST /record/stop {events} ends it with the player's event log, and
POST /record/cancel / /record/restart {source} discard it (and start anew);
record.sh logs to record.log. GET /devices lists the audio inputs and
POST /voicetest {mic, recorder} runs one voicetest.py for the test bench.
POST /delete {id} moves a take's MP4 to media/.trash/ and drops its row.
"""
import http.server
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse

import voicetest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "media")
recorder = None  # the running record.sh, if any
testing = threading.Lock()  # held while voicetest.py has the mic
SOURCES = os.path.join(HERE, "sources")  # source movies, usually symlinks


PHASES = {"starting": "Starting the recorder…",
          "recording": "Recording: click Stop to finish",
          "rendering": "Rendering the take…",
          "naming": "Naming the take from its narration…"}


def cancel_recording(timeout=5):
    # Ask record.sh to end the take without rendering, and wait for it to exit.
    open(os.path.join(ROOT, ".record-cancel"), "w").close()
    try:
        recorder.wait(timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def record_status(running):
    # record.sh writes its phase to media/.record-state and removes it on exit.
    if not running:
        return {"phase": None, "label": None}
    try:
        phase = open(os.path.join(ROOT, ".record-state")).read().strip()
    except OSError:
        phase = "starting"
    return {"phase": phase, "label": PHASES.get(phase, phase)}


def sources():
    names = os.listdir(SOURCES) if os.path.isdir(SOURCES) else []
    return sorted(n for n in names if n.lower().endswith((".mp4", ".mov", ".m4v"))
                  and os.path.isfile(os.path.join(SOURCES, n)))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/wav", ".wav")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def translate_path(self, path):
        # /sources/<name> streams a source movie (via its symlink), but only a
        # name GET /sources lists; everything else maps into media/.
        name = urllib.parse.unquote(path.split("?", 1)[0])
        if name.startswith("/sources/"):
            name = name[len("/sources/"):]
            return os.path.join(SOURCES, name) if name in sources() else os.path.join(ROOT, ".no-such-file")
        return super().translate_path(path)

    def send_head(self):
        path = self.translate_path(self.path)
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if not m or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
        end = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end:
            self.send_error(416)
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    # The app runs on Bram's origin, so its POSTs here are cross-origin and
    # preflighted. Allow whatever headers it asks for: XMLUI adds its own
    # x-ue-client-tx-id to every request, not just Content-Type.
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         self.headers.get("Access-Control-Request-Headers", "Content-Type"))
        self.end_headers()

    def do_GET(self):
        if self.path == "/devices":
            return self.send_json(200, [{"name": n} for n in voicetest.devices()])
        if self.path == "/record/status":
            return self.send_json(200, record_status(self.recording()))
        if self.path == "/sources":
            return self.send_json(200, [{"name": n} for n in sources()])
        super().do_GET()

    def do_POST(self):
        if self.path == "/record":
            return self.start_recording()
        if self.path == "/record/restart":
            return self.start_recording(restart=True)
        if self.path == "/record/cancel":
            if not self.recording():
                return self.send_json(409, {"error": "nothing is recording"})
            return self.send_json(200, {"cancelled": cancel_recording()})
        if self.path == "/record/stop":
            return self.stop_recording()
        if self.path == "/voicetest":
            return self.run_voicetest()
        if self.path == "/delete":
            return self.delete_take()
        self.send_json(404, {"error": "not found"})

    def delete_take(self):
        # Move the take's MP4 to media/.trash/ first; drop the row only if that worked.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        row = db.execute("SELECT file FROM takes WHERE id = ?", (body.get("id"),)).fetchone()
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        src, trashed = os.path.join(ROOT, row[0]), None
        if os.path.exists(src):
            trash = os.path.join(ROOT, ".trash")
            os.makedirs(trash, exist_ok=True)
            name = row[0]
            if os.path.exists(os.path.join(trash, name)):
                stem, ext = os.path.splitext(name)
                name = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
            try:
                shutil.move(src, os.path.join(trash, name))
            except OSError as e:
                return self.send_json(500, {"error": f"couldn't move {row[0]} to .trash: {e}"})
            trashed = f".trash/{name}"
        db.execute("DELETE FROM takes WHERE id = ?", (body["id"],))
        db.commit()
        self.send_json(200, {"deleted": body["id"], "trashed": trashed})

    def recording(self):
        return recorder is not None and recorder.poll() is None

    def start_recording(self, restart=False):
        # record.sh runs one take: voice until Stop -> render -> register.
        # restart=True first discards the running take (no render, no row).
        global recorder
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        source = body.get("source")
        if not source and len(sources()) == 1:
            source = sources()[0]  # nothing picked, and there's only one to pick
        if source not in sources():
            return self.send_json(400, {"error": f"no source movie named {source!r} in sources/"})
        if restart and self.recording() and not cancel_recording():
            return self.send_json(409, {"error": "the running take didn't stop in time"})
        if self.recording() or testing.locked():
            return self.send_json(409, {"error": "the mic is busy (recording or testing)"})
        log = open(os.path.join(HERE, "record.log"), "a")
        recorder = subprocess.Popen([os.path.join(HERE, "record.sh"), os.path.join(SOURCES, source)],
                                    cwd=HERE, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.send_json(202, {"started": True})

    def stop_recording(self):
        # The page's MediaPlayer event log becomes the session's events.json;
        # then .record-stop tells record.sh the take is over.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not self.recording():
            return self.send_json(409, {"error": "nothing is recording"})
        try:
            session = open(os.path.join(ROOT, ".record-session")).read().strip()
        except OSError:
            return self.send_json(409, {"error": "the recorder hasn't started yet"})
        json.dump(body.get("events", []), open(os.path.join(session, "events.json"), "w"), indent=1)
        # What the source player reported at Stop, so an empty take carries evidence.
        json.dump(body.get("diag", {}), open(os.path.join(session, "diag.json"), "w"), indent=1)
        open(os.path.join(ROOT, ".record-stop"), "w").close()
        self.send_json(200, {"stopped": True, "events": len(body.get("events", []))})

    def run_voicetest(self):
        # One 10s test at a time, never during a take; returns when it's measured.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.recording() or not testing.acquire(blocking=False):
            return self.send_json(409, {"error": "the mic is busy (recording or testing)"})
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, "voicetest.py"),
                                str(body.get("mic", "")), str(body.get("recorder", ""))],
                               cwd=HERE, capture_output=True, text=True)
        finally:
            testing.release()
        if r.returncode:
            return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["voicetest failed"])[-1]})
        self.send_json(200, json.loads(r.stdout))

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
