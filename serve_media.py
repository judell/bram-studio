#!/usr/bin/env python3
"""Serve media/ with video content types and HTTP byte ranges.

    python3 serve_media.py [port]      (default 8765)

Bram's loopback serves project files but has no video content types and
no Range support, so a browser would download an MP4 instead of playing
it (and couldn't seek). This fills that gap until Bram does it itself.

POST /record starts record.sh (one take) for the app's Record button;
its output goes to record.log. GET /devices lists the audio inputs and
POST /voicetest {mic, recorder} runs one voicetest.py for the test bench.
"""
import http.server
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading

import voicetest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "media")
recorder = None  # the running record.sh, if any
testing = threading.Lock()  # held while voicetest.py has the mic
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/wav", ".wav")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

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
        super().do_GET()

    def do_POST(self):
        if self.path == "/record":
            return self.start_recording()
        if self.path == "/voicetest":
            return self.run_voicetest()
        self.send_json(404, {"error": "not found"})

    def recording(self):
        return recorder is not None and recorder.poll() is None

    def start_recording(self):
        # record.sh runs one take: QuickTime session -> close -> render -> register.
        global recorder
        if self.recording() or testing.locked():
            return self.send_json(409, {"error": "the mic is busy (recording or testing)"})
        log = open(os.path.join(HERE, "record.log"), "a")
        recorder = subprocess.Popen([os.path.join(HERE, "record.sh")], cwd=HERE, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.send_json(202, {"started": True})

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
