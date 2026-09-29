#!/usr/bin/env python3
"""Serve media/ with video content types and HTTP byte ranges.

    python3 serve_media.py [port]      (default 8765)

Bram's loopback serves project files but has no video content types and
no Range support, so a browser would download an MP4 instead of playing
it (and couldn't seek). This fills that gap until Bram does it itself.

GET /sources lists the movies in sources/ and on the Desktop (newest first)
and /sources/<name> streams one;
GET /record/status reports a running take's phase; POST /record {source}
starts record.sh (one take of that movie) for the app's Record button,
POST /record/stop {events} ends it with the player's event log, and
POST /record/cancel / /record/restart {source} discard it (and start anew);
record.sh logs to record.log. GET /devices lists the audio inputs and
POST /voicetest {mic, recorder} runs one voicetest.py for the test bench.
POST /delete {id} moves a take's MP4 to media/.trash/ and drops its row,
keeping both in a .trash/<stem>.json that POST /undelete {undo} restores;
POST /reorder {ids} saves the takes list's drag order; POST /export joins
all takes in that order into media/exports/<stamp>-takes.mp4.
POST /callouts/add {take_id, text, x1, y1, x2, y2, t_in, t_out, tail},
/callouts/update {id, ...the same} and /callouts/delete {id} edit a take's
callouts; POST /callouts/preview {...a callout} draws a still of it into
media/.preview/<take id>.png, and POST /callouts/sprite {...} just the callout,
with where it goes, for the take player's PointerLayer anchors (#3922);
POST /overlay/render {id} burns the take's text
in with overlay.py (from its clean copy).
GET /events is a server-sent-events stream: {"changed": "takes"|"sources"|
"record"|"exports"} whenever watch_changes() sees one of them change, so the
page refetches then instead of polling.
"""
import hashlib
import http.server
import json
import mimetypes
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import overlay
import voicetest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "media")
recorder = None  # the running record.sh, if any
testing = threading.Lock()  # held while voicetest.py has the mic
SOURCES = os.path.join(HERE, "sources")  # source movies, usually symlinks
DESKTOP = os.path.expanduser("~/Desktop")  # where recordings usually land


PHASES = {"starting": "Starting the recorder…",
          "recording": "Recording: click Stop to finish",
          "rendering": "Rendering the take…",
          "naming": "Naming the take from its narration…"}


exporting = threading.Lock()  # one export at a time
overlaying = threading.Lock()  # one overlay.py render at a time
SPRITE_WHERE = {}  # recording sprite tag -> where it goes (0-1 picture box)


def probe(path, entries, stream=None):
    args = ["ffprobe", "-v", "error", *(["-select_streams", stream] if stream else []),
            "-show_entries", entries, "-of", "csv=p=0", path]
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def stream_signature(path):
    # What must match for a stream-copy join: codecs, size, rates, channels.
    return probe(path, "stream=codec_name,width,height,r_frame_rate,sample_rate,channels")


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


EVENT_QUEUES = set()  # one queue per page listening on GET /events
EVENT_LOCK = threading.Lock()


def notify(kind):
    with EVENT_LOCK:
        for q in EVENT_QUEUES:
            q.put(kind)


def fingerprints():
    # What each of the page's lists is built from, cheaply: the recording
    # phase, studio.db (takes, callouts; register.py writes it too), the two
    # source folders and the exports folder. A change means "refetch that".
    def mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None
    return {"record": json.dumps(record_status(recorder is not None and recorder.poll() is None)),
            "takes": mtime(os.path.join(HERE, "studio.db")),
            "sources": (mtime(SOURCES), mtime(DESKTOP)),
            "exports": (mtime(os.path.join(ROOT, "exports")), mtime(DESKTOP))}


def watch_changes():
    # Once a second, in one place, instead of every open page polling each list.
    last = fingerprints()
    while True:
        time.sleep(1)
        try:
            now = fingerprints()
        except Exception as e:  # keep watching; a bad tick shouldn't end the stream
            print(f"watch_changes: {e}", flush=True)
            continue
        for kind, value in now.items():
            if value != last.get(kind):
                notify(kind)
        last = now


EXPORT_SECONDS = {}  # (export path, mtime) -> its length in seconds
EXPORT_NAME = re.compile(r"\d{8}-\d{6}-takes\.mp4$")  # what Export names its output


def export_entry(path, url, where):
    # {name, url, created, seconds, where} for one listed file. created comes
    # from the name's leading <YYYYMMDD-HHMMSS> stamp (Export's names), else the
    # file's mtime; lengths are probed once per file version.
    name = os.path.basename(path)
    key = (path, os.path.getmtime(path))
    if key not in EXPORT_SECONDS:
        EXPORT_SECONDS[key] = round(float(probe(path, "format=duration") or 0), 1)
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
    created = (f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]}" if m
               else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(key[1])))
    return {"name": name, "url": url, "created": created, "seconds": EXPORT_SECONDS[key], "where": where}


def level_report(outdir, name):
    # "before -> after" loudness saved by POST /exports/level beside a leveled file.
    try:
        with open(os.path.join(outdir, f".{name}.json")) as f:
            r = json.load(f)
        b, a = r["before"], r["after"]
        return f"{b['I']} → {a['I']} LUFS, range {b['LRA']} → {a['LRA']} LU"
    except (OSError, ValueError, KeyError):
        return ""


def list_exports():
    # Three stages of the finishing workflow, each newest first:
    #   exports: what Export makes (<YYYYMMDD-HHMMSS>-takes.mp4, EXPORT_NAME);
    #   leveled: leveled-*.mp4, written by POST /exports/level (level_edit.py);
    #   edited:  any other .mp4 in media/exports/ (saved there from another
    #            editor, whatever its name), plus edited-*.mp4 at the top of
    #            the Desktop (served there via /sources/<name>).
    # Hidden names (.<name>.part.mp4 while being written) are skipped.
    outdir = os.path.join(ROOT, "exports")
    lists = {"exports": [], "edited": [], "leveled": []}
    for name in os.listdir(outdir) if os.path.isdir(outdir) else []:
        path = os.path.join(outdir, name)
        if name.startswith(".") or not name.endswith(".mp4") or not os.path.isfile(path):
            continue
        entry = export_entry(path, f"http://127.0.0.1:{PORT}/exports/{name}", "exports")
        if EXPORT_NAME.match(name):
            lists["exports"].append(entry)
        elif name.startswith("leveled-"):
            entry["report"] = level_report(outdir, name)
            lists["leveled"].append(entry)
        else:
            lists["edited"].append(entry)
    for name in os.listdir(DESKTOP) if os.path.isdir(DESKTOP) else []:
        path = os.path.join(DESKTOP, name)
        if name.startswith("edited-") and name.endswith(".mp4") and os.path.isfile(path):
            lists["edited"].append(export_entry(
                path, f"http://127.0.0.1:{PORT}/sources/{urllib.parse.quote(name)}", "desktop"))
    return {k: sorted(v, key=lambda e: e["created"], reverse=True) for k, v in lists.items()}


def source_paths():
    # Offered name -> file: videos in sources/ (links made by hand) and at the
    # top level of the Desktop, newest first. The same file reached both ways
    # is listed once; on a name clash sources/ wins. Broken links are skipped.
    found, seen = {}, set()
    for folder in (SOURCES, DESKTOP):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for n in names:
            path = os.path.join(folder, n)
            if (not n.lower().endswith((".mp4", ".mov", ".m4v")) or n.startswith(".")
                    or not os.path.isfile(path) or n in found or os.path.realpath(path) in seen):
                continue
            found[n] = path
            seen.add(os.path.realpath(path))
    return dict(sorted(found.items(), key=lambda kv: -os.path.getmtime(kv[1])))


def sources():
    return list(source_paths())
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/wav", ".wav")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def translate_path(self, path):
        # /sources/<name> streams a source movie (from sources/ or the
        # Desktop), but only a name GET /sources lists; everything else maps
        # into media/.
        name = urllib.parse.unquote(path.split("?", 1)[0])
        if name.startswith("/clean/"):
            # A take without its burned-in text (overlay.py's clean copy), for
            # the callout editor, which draws the text live; the take itself
            # if it has never had text. Only plain take file names.
            name = name[len("/clean/"):]
            if "/" in name or name.startswith(".") or not os.path.isfile(os.path.join(ROOT, name)):
                return os.path.join(ROOT, ".no-such-file")
            clean = os.path.join(ROOT, ".clean", name)
            return clean if os.path.isfile(clean) else os.path.join(ROOT, name)
        if name.startswith("/sources/"):
            name = name[len("/sources/"):]
            return source_paths().get(name, os.path.join(ROOT, ".no-such-file"))
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
        if self.path == "/exports":
            return self.send_json(200, list_exports())
        if self.path == "/events":
            return self.stream_events()
        super().do_GET()

    def stream_events(self):
        # Server-sent events for the page's <EventSource>: data: {"changed":
        # "takes"|"sources"|"record"|"exports"} as watch_changes() sees them,
        # and a comment line every 15 s so an idle connection stays open.
        q = queue.Queue()
        with EVENT_LOCK:
            EVENT_QUEUES.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"data: {json.dumps({'changed': q.get(timeout=15)})}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with EVENT_LOCK:
                EVENT_QUEUES.discard(q)

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
        if self.path == "/undelete":
            return self.undelete_take()
        if self.path == "/reorder":
            return self.reorder_takes()
        if self.path == "/export":
            return self.export_takes()
        if self.path == "/exports/delete":
            return self.delete_export()
        if self.path == "/exports/level":
            return self.level_export()
        if self.path == "/callouts/add":
            return self.add_callout()
        if self.path == "/callouts/update":
            return self.add_callout(update=True)
        if self.path == "/callouts/preview":
            return self.preview_callout()
        if self.path == "/callouts/sprite":
            return self.callout_sprite()
        if self.path == "/callouts/sprites":
            return self.callout_sprites()
        if self.path == "/annotations/sprites":
            return self.annotation_sprites()
        if self.path == "/callouts/delete":
            return self.delete_callout()
        if self.path == "/overlay/render":
            return self.render_overlay()
        self.send_json(404, {"error": "not found"})

    @staticmethod
    def listed_export(name, where):
        # The file a {name, where} from the exports lists names, or None: plain
        # .mp4 names only; on the Desktop, only edited-*.mp4 at the top level.
        if "/" in name or name.startswith(".") or not name.endswith(".mp4"):
            return None
        if where == "desktop":
            path = os.path.join(DESKTOP, name) if name.startswith("edited-") else None
        else:
            path = os.path.join(ROOT, "exports", name)
        return path if path and os.path.isfile(path) else None

    def delete_export(self):
        # Move one listed file to media/.trash/ (like a deleted take), with a
        # leveled file's loudness report.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = str(body.get("name", ""))
        src = self.listed_export(name, body.get("where"))
        if not src:
            return self.send_json(404, {"error": f"no export named {name!r}"})
        trash = os.path.join(ROOT, ".trash")
        os.makedirs(trash, exist_ok=True)
        dest = os.path.join(trash, name)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            dest = os.path.join(trash, f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{ext}")
        shutil.move(src, dest)
        report = os.path.join(os.path.dirname(src), f".{name}.json")
        if os.path.exists(report):
            shutil.move(report, os.path.join(trash, f".{os.path.basename(dest)}.json"))
        self.send_json(200, {"deleted": name, "trashed": f".trash/{os.path.basename(dest)}"})

    def level_export(self):
        # Run level_edit.py on an edited file (any listed .mp4 that isn't an
        # Export or already leveled) into media/exports/leveled-<name>.mp4, an
        # edited- prefix dropped (written hidden, then renamed, like Export),
        # keeping its before/after loudness in .leveled-<name>.mp4.json for the
        # list. Leveling again replaces the earlier result.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = str(body.get("name", ""))
        src = self.listed_export(name, body.get("where"))
        if not src or EXPORT_NAME.match(name) or name.startswith("leveled-"):
            return self.send_json(404, {"error": f"no edited export named {name!r}"})
        outdir = os.path.join(ROOT, "exports")
        os.makedirs(outdir, exist_ok=True)
        leveled = "leveled-" + (name[len("edited-"):] if name.startswith("edited-") else name)
        part = os.path.join(outdir, f".{leveled[:-4]}.part.mp4")
        r = subprocess.run([sys.executable, os.path.join(HERE, "level_edit.py"), src, part],
                           capture_output=True, text=True)
        if r.returncode:
            if os.path.exists(part):
                os.remove(part)
            return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["leveling failed"])[-1]})
        report = {}
        for line in r.stdout.splitlines():
            m = re.match(r"(before|after)\s+(-?[\d.]+) LUFS\s+range\s+([\d.]+) LU\s+true peak\s+(-?[\d.]+) dBFS", line)
            if m:
                report[m[1]] = {"I": float(m[2]), "LRA": float(m[3]), "TP": float(m[4])}
        with open(os.path.join(outdir, f".{leveled}.json"), "w") as f:
            json.dump(report, f)
        os.replace(part, os.path.join(outdir, leveled))
        self.send_json(200, {"leveled": leveled, "report": report})

    def export_takes(self):
        # All takes, in list order, as one MP4 in media/exports/. Identical
        # streams (the usual case: one render.py, one source) join by stream
        # copy; otherwise re-encode, fitting each take to the first one's size.
        if not exporting.acquire(blocking=False):
            return self.send_json(409, {"error": "an export is already running"})
        try:
            db = sqlite3.connect(os.path.join(HERE, "studio.db"))
            names = [f for (f,) in db.execute("SELECT file FROM takes ORDER BY position, created_at DESC")]
            files = [os.path.join(ROOT, f) for f in names if os.path.isfile(os.path.join(ROOT, f))]
            if not files:
                return self.send_json(400, {"error": "no takes to export"})
            outdir = os.path.join(ROOT, "exports")
            os.makedirs(outdir, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-takes.mp4"
            # Written under a hidden name and renamed when complete, so the
            # /events watcher never announces (and the list never probes) a
            # half-written file.
            final, out = os.path.join(outdir, name), os.path.join(outdir, f".{name[:-4]}.part.mp4")
            copied = len({stream_signature(f) for f in files}) == 1
            if copied:
                lst = out + ".txt"
                with open(lst, "w") as fh:
                    fh.writelines(f"file '{f}'\n" for f in files)
                cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out]
            else:
                W, H = probe(files[0], "stream=width,height", "v:0").split(",")
                chains = "".join(
                    f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,fps=25[v{i}];[{i}:a]aresample=48000,aformat=channel_layouts=mono[a{i}];"
                    for i in range(len(files)))
                joined = "".join(f"[v{i}][a{i}]" for i in range(len(files)))
                cmd = ["ffmpeg", "-v", "error", "-y", *[a for f in files for a in ("-i", f)], "-filter_complex",
                       f"{chains}{joined}concat=n={len(files)}:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-tune", "stillimage",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", out]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if copied:
                os.remove(lst)
            if r.returncode:
                if os.path.exists(out):
                    os.remove(out)
                return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["export failed"])[-1]})
            os.replace(out, final)
            out = final
            seconds = float(probe(out, "format=duration") or 0)
            self.send_json(200, {"url": f"http://127.0.0.1:{PORT}/exports/{name}", "file": f"exports/{name}",
                                 "takes": len(files), "seconds": round(seconds, 1), "copied": copied})
        finally:
            exporting.release()

    def callout_fields(self, body):
        # A callout's or shape's fields from a request, checked: (take_id,
        # text, box, t_in, t_out, tail, shape), clamped to 0-1, or an error.
        # shape (overlay.SHAPES) makes it a shape: no text or tail, and an
        # arrow keeps its direction (tail x1,y1 to tip x2,y2); other boxes get
        # their corners ordered.
        shape = body.get("shape") if body.get("shape") in overlay.SHAPES else None
        text = "" if shape else str(body.get("text", "")).strip()
        try:
            x1, y1, x2, y2, t_in, t_out = (float(body[k]) for k in ("x1", "y1", "x2", "y2", "t_in", "t_out"))
        except (KeyError, TypeError, ValueError):
            return None, "a callout or shape needs x1, y1, x2, y2, t_in and t_out"
        if not isinstance(body.get("take_id"), int):
            return None, "a callout or shape needs a take"
        if not shape and not text:
            return None, "a callout needs some text"
        if t_out <= t_in:
            return None, "the out point must come after the in point"
        clamp = lambda v: max(0.0, min(1.0, v))
        if shape in ("arrow", "line"):
            box = (clamp(x1), clamp(y1), clamp(x2), clamp(y2))
        else:
            box = (clamp(min(x1, x2)), clamp(min(y1, y2)), clamp(max(x1, x2)), clamp(max(y1, y2)))
        # A speech-bubble tail at one of the box's compass points, or none.
        tail = body.get("tail") if not shape and body.get("tail") in overlay.TAILS else None
        return (body["take_id"], text, box, max(0.0, t_in), t_out, tail, shape), None

    def add_callout(self, update=False):
        # One callout: text in a box (0-1 picture coordinates, from the take
        # player's PointerLayer) shown from t_in to t_out (take seconds). With
        # update, the callout with body["id"] is replaced.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        fields, err = self.callout_fields(body)
        if err:
            return self.send_json(400, {"error": err})
        take_id, text, box, t_in, t_out, tail, shape = fields
        kind = "shape" if shape else "callout"
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        if not db.execute("SELECT 1 FROM takes WHERE id = ?", (take_id,)).fetchone():
            return self.send_json(404, {"error": f"no take with id {take_id}"})
        if update:
            n = db.execute("UPDATE overlays SET kind = ?, text = ?, x1 = ?, y1 = ?, x2 = ?, y2 = ?, t_in = ?, "
                           "t_out = ?, tail = ?, shape = ? WHERE id = ? AND take_id = ? AND kind IN ('callout', 'shape')",
                           (kind, text, *box, t_in, t_out, tail, shape, body.get("id"), take_id)).rowcount
            db.commit()
            return self.send_json(200 if n else 404, {"id": body.get("id")} if n else {"error": "no such callout"})
        cur = db.execute("INSERT INTO overlays (take_id, kind, text, x1, y1, x2, y2, t_in, t_out, tail, shape) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (take_id, kind, text, *box, t_in, t_out, tail, shape))
        db.commit()
        self.send_json(200, {"id": cur.lastrowid})

    def callout_sprite(self):
        # The callout being edited as a transparent image of just its box,
        # tail and text (overlay.sprite, the drawing Apply uses) plus where it
        # goes in 0-1 picture coordinates, for PointerLayer's anchors to place
        # over the video (xmlui-org/xmlui#3922). Named by its content, so an
        # unchanged callout keeps its URL.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        fields, err = self.callout_fields({"t_in": 0, "t_out": 1, **body})
        if err:
            return self.send_json(400, {"error": err})
        take_id, text, box, _, _, tail, shape = fields
        item = {"text": text, "x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3], "tail": tail,
                "shape": shape, "frame": bool(body.get("frame")) and not shape}
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        try:
            self.send_json(200, self.sprite_file(db, take_id, item))
        except LookupError as e:
            self.send_json(404, {"error": str(e)})

    def sprite_file(self, db, take_id, item):
        # Draw one callout sprite into media/.preview/sprite-<take>-<tag>.png,
        # named by its content (an unchanged callout keeps its URL), and return
        # its URL and where it goes.
        tag = hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()[:10]
        outdir = os.path.join(ROOT, ".preview")
        path = os.path.join(outdir, f"sprite-{take_id}-{tag}.png")
        img, where = overlay.sprite(db, take_id, item, frame=item.get("frame", False))
        os.makedirs(outdir, exist_ok=True)
        if not os.path.exists(path):
            img.save(path)
        return {"url": f"http://127.0.0.1:{PORT}/.preview/sprite-{take_id}-{tag}.png", "tag": tag, **where}

    def annotation_sprites(self):
        # The callouts and shapes placed while recording, as sprites over the
        # source player (there is no take yet): {source, items: [{cid, shape,
        # text, tail, x1, y1, x2, y2, frame}]} -> [{cid, url, x, y, width,
        # height}], sized from the source video (the take's size too). Files
        # are named by content, so unchanged items keep their URLs.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        path = source_paths().get(body.get("source"))
        if not path:
            return self.send_json(404, {"error": f"no source movie named {body.get('source')!r}"})
        size = overlay.video_size(path)
        outdir = os.path.join(ROOT, ".preview")
        os.makedirs(outdir, exist_ok=True)
        out = []
        for it in body.get("items") or []:
            try:
                x1, y1, x2, y2 = (max(0.0, min(1.0, float(it[k]))) for k in ("x1", "y1", "x2", "y2"))
            except (KeyError, TypeError, ValueError):
                continue
            shape = it.get("shape") if it.get("shape") in overlay.SHAPES else None
            item = {"text": "" if shape else str(it.get("text") or ""), "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "tail": it.get("tail") if not shape and it.get("tail") in overlay.TAILS else None,
                    "shape": shape, "frame": bool(it.get("frame")) and not shape, "size": list(size)}
            tag = hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()[:10]
            name = f"sprite-src-{tag}.png"
            where = None
            if not os.path.exists(os.path.join(outdir, name)) or tag not in SPRITE_WHERE:
                img, where = overlay.sprite(None, None, item, frame=item["frame"], size=size)
                img.save(os.path.join(outdir, name))
                SPRITE_WHERE[tag] = where
            out.append({"cid": it.get("cid"), "url": f"http://127.0.0.1:{PORT}/.preview/{name}",
                        **SPRITE_WHERE[tag]})
        # Recording sprites older than ten minutes are stale.
        for name in os.listdir(outdir):
            p = os.path.join(outdir, name)
            if name.startswith("sprite-src-") and time.time() - os.path.getmtime(p) > 600:
                os.remove(p)
                SPRITE_WHERE.pop(name[len("sprite-src-"):-len(".png")], None)
        self.send_json(200, out)

    def callout_sprites(self):
        # Every saved callout of a take as a sprite, with its in and out
        # points, for the editor to show over the take's clean copy.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        take_id = body.get("take_id")
        if not isinstance(take_id, int):
            return self.send_json(400, {"error": "take_id must be a take id"})
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        rows = db.execute("SELECT id, text, x1, y1, x2, y2, t_in, t_out, tail, shape FROM overlays "
                          "WHERE take_id = ? AND kind IN ('callout', 'shape') ORDER BY t_in", (take_id,)).fetchall()
        out = []
        try:
            for cid, text, x1, y1, x2, y2, t_in, t_out, tail, shape in rows:
                item = {"text": text, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "tail": tail, "shape": shape,
                        "frame": False}
                out.append({"id": cid, "t_in": t_in, "t_out": t_out, **self.sprite_file(db, take_id, item)})
        except LookupError as e:
            return self.send_json(404, {"error": str(e)})
        # Prune this take's other sprites (edits since superseded); ones under
        # a minute old may still be on screen in the editor.
        outdir, keep = os.path.join(ROOT, ".preview"), {f"sprite-{take_id}-{o['tag']}.png" for o in out}
        for name in os.listdir(outdir) if os.path.isdir(outdir) else []:
            path = os.path.join(outdir, name)
            if (name.startswith(f"sprite-{take_id}-") and name not in keep
                    and time.time() - os.path.getmtime(path) > 60):
                os.remove(path)
        self.send_json(200, out)

    def preview_callout(self):
        # A still of the take with this callout (and whatever else shows then),
        # drawn as overlay.py render would: media/.preview/<take id>.png.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        fields, err = self.callout_fields(body)
        if err:
            return self.send_json(400, {"error": err})
        take_id, text, box, t_in, t_out, tail, _ = fields
        item = {"id": body.get("id"), "text": text, "x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3],
                "t_in": t_in, "t_out": t_out, "tail": tail}
        out = os.path.join(ROOT, ".preview", f"{take_id}.png")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(item, fh)
        try:
            r = subprocess.run(["python3", os.path.join(HERE, "overlay.py"), "preview", str(take_id), fh.name, out],
                               cwd=HERE, capture_output=True, text=True)
        finally:
            os.remove(fh.name)
        if r.returncode:
            return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["preview failed"])[-1]})
        self.send_json(200, {"url": f"http://127.0.0.1:{PORT}/.preview/{take_id}.png?v={time.time():.3f}"})

    def delete_callout(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        n = db.execute("DELETE FROM overlays WHERE id = ? AND kind IN ('callout', 'shape')", (body.get("id"),)).rowcount
        db.commit()
        self.send_json(200 if n else 404, {"deleted": body.get("id")} if n else {"error": "no such callout"})

    def render_overlay(self):
        # Burn a take's text in (overlay.py), from its clean copy.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not isinstance(body.get("id"), int):
            return self.send_json(400, {"error": "id must be a take id"})
        if not overlaying.acquire(blocking=False):
            return self.send_json(409, {"error": "a take is already being rendered"})
        try:
            r = subprocess.run(["python3", os.path.join(HERE, "overlay.py"), "render", str(body["id"])],
                               cwd=HERE, capture_output=True, text=True)
        finally:
            overlaying.release()
        if r.returncode:
            return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["render failed"])[-1]})
        self.send_json(200, json.loads(r.stdout.strip().splitlines()[-1]))

    def reorder_takes(self):
        # The takes list's drag order: each id's index becomes its position.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        ids = [i for i in body.get("ids", []) if isinstance(i, int)]
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        db.executemany("UPDATE takes SET position = ? WHERE id = ?", [(pos, tid) for pos, tid in enumerate(ids)])
        db.commit()
        self.send_json(200, {"reordered": len(ids)})

    def delete_take(self):
        # Move the take's MP4 to media/.trash/ first; drop the row only if that worked.
        # Beside it, <name>.json keeps the take's row and its overlays for /undelete.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM takes WHERE id = ?", (body.get("id"),)).fetchone()
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        trash = os.path.join(ROOT, ".trash")
        os.makedirs(trash, exist_ok=True)
        name = row["file"]
        stem, ext = os.path.splitext(name)
        if any(os.path.exists(os.path.join(trash, n)) for n in (name, f"{stem}.json")):
            stem = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}"
            name = f"{stem}{ext}"
        saved = {"take": dict(row), "files": {},
                 "overlays": [dict(r) for r in db.execute(
                     "SELECT * FROM overlays WHERE take_id = ? ORDER BY id", (row["id"],))]}
        src, trashed = os.path.join(ROOT, row["file"]), None
        if os.path.exists(src):
            try:
                shutil.move(src, os.path.join(trash, name))
            except OSError as e:
                return self.send_json(500, {"error": f"couldn't move {row['file']} to .trash: {e}"})
            trashed = f".trash/{name}"
            saved["files"][row["file"]] = name
            # Its text-free copy (overlay.py) goes along, beside it.
            clean = os.path.join(ROOT, ".clean", row["file"])
            if os.path.exists(clean):
                shutil.move(clean, os.path.join(trash, f"{stem}-clean{ext}"))
                saved["files"][f".clean/{row['file']}"] = f"{stem}-clean{ext}"
        undo = f"{stem}.json"
        with open(os.path.join(trash, undo), "w") as f:
            json.dump(saved, f, indent=1)
        db.execute("DELETE FROM takes WHERE id = ?", (row["id"],))
        db.execute("DELETE FROM overlays WHERE take_id = ?", (row["id"],))
        db.commit()
        self.send_json(200, {"deleted": row["id"], "name": row["name"], "trashed": trashed, "undo": undo})

    def undelete_take(self):
        # Undo a take delete from its media/.trash/<stem>.json: files back where
        # they were, then the take (same id if still free) and its overlays.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        undo = str(body.get("undo", ""))
        trash = os.path.join(ROOT, ".trash")
        path = os.path.join(trash, undo)
        if "/" in undo or undo.startswith(".") or not undo.endswith(".json") or not os.path.isfile(path):
            return self.send_json(404, {"error": f"nothing to undo named {undo!r}"})
        with open(path) as f:
            saved = json.load(f)
        files = saved["files"]
        taken = [orig for orig in files if os.path.exists(os.path.join(ROOT, orig))]
        if taken:
            return self.send_json(409, {"error": f"can't undo: {', '.join(taken)} exists again"})
        for orig, trashed in files.items():
            os.makedirs(os.path.dirname(os.path.join(ROOT, orig)), exist_ok=True)
            shutil.move(os.path.join(trash, trashed), os.path.join(ROOT, orig))
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        take = dict(saved["take"])
        if db.execute("SELECT 1 FROM takes WHERE id = ?", (take["id"],)).fetchone():
            del take["id"]
        cols = list(take)
        cur = db.execute(f"INSERT INTO takes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [take[c] for c in cols])
        take_id = cur.lastrowid
        for o in saved["overlays"]:
            o = {k: v for k, v in o.items() if k != "id"}
            o["take_id"] = take_id
            db.execute(f"INSERT INTO overlays ({', '.join(o)}) VALUES ({', '.join('?' * len(o))})", list(o.values()))
        db.commit()
        os.remove(path)
        self.send_json(200, {"restored": take_id, "name": take["name"], "overlays": len(saved["overlays"])})

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
            return self.send_json(400, {"error": f"no source movie named {source!r} in sources/ or on the Desktop"})
        if restart and self.recording() and not cancel_recording():
            return self.send_json(409, {"error": "the running take didn't stop in time"})
        if self.recording() or testing.locked():
            return self.send_json(409, {"error": "the mic is busy (recording or testing)"})
        log = open(os.path.join(HERE, "record.log"), "a")
        recorder = subprocess.Popen([os.path.join(HERE, "record.sh"), source_paths()[source]],
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


class AdoptedRecorder:
    # A record.sh that was already running when this server started (it was
    # restarted mid-take): stands in for the Popen handle, so Stop, Cancel and
    # the status line keep working instead of reporting "nothing is recording".
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        try:
            os.kill(self.pid, 0)
            return None
        except OSError:
            return 0

    def wait(self, timeout=None):
        deadline = time.time() + (timeout if timeout is not None else 1e9)
        while self.poll() is None:
            if time.time() > deadline:
                raise subprocess.TimeoutExpired("record.sh", timeout)
            time.sleep(0.1)
        return 0


def adopt_running_take():
    # record.sh writes its session dir to media/.record-session and the voice
    # recorder's pid to <session>/pids; the recorder's parent is record.sh.
    try:
        session = open(os.path.join(ROOT, ".record-session")).read().strip()
        rec_pid = int(open(os.path.join(session, "pids")).read().split()[0])
        ppid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(rec_pid)], capture_output=True,
                                  text=True).stdout.strip())
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(ppid)], capture_output=True, text=True).stdout
    except (OSError, ValueError):
        return None
    if "record.sh" not in cmd:
        return None
    print(f"adopted the running take: record.sh pid {ppid}, session {session}", flush=True)
    return AdoptedRecorder(ppid)


# The callouts query reads overlays, so it must exist before the page asks.
with sqlite3.connect(os.path.join(HERE, "studio.db")) as _db:
    overlay.ensure_schema(_db)
recorder = adopt_running_take()
threading.Thread(target=watch_changes, daemon=True).start()
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
