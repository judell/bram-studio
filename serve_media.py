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
POST /record/event {events} logs events as they happen,
POST /record/stop {events} ends it with the player's event log, and
POST /record/cancel / /record/restart {source} discard it (and start anew);
record.sh logs to record.log. GET /devices lists the audio inputs and
POST /voicetest {mic, recorder} runs one voicetest.py for the test bench.
POST /delete {id} moves a take's MP4 to media/.trash/ and drops its row,
keeping both in a .trash/<stem>.json that POST /undelete {undo} restores;
POST /takes/cut {id, start, end} removes that stretch from a take (and from
its clean copy, shifting its overlays), POST /takes/pause {id, at, seconds}
holds the frame there with silence; GET /takes/edits?id= lists a take's
edits, and POST /takes/undo {id} and /takes/redo {id} step back and forth
through them;
POST /narrate/start {id, start, end} opens the mic to narrate over that
stretch of a take, /narrate/go marks the moment the page starts playing it,
and /narrate/stop replaces the take's audio there with what was said (an
edit like the others); POST /narrate/remove {id} takes a narration back out
at any time, from the audio it replaced;
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
import collections
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
import traceback
import urllib.error
import urllib.parse
import urllib.request

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
    push({"changed": kind})


def push(message):
    # Any JSON-able message to every page on GET /events: {"changed": kind}
    # from the watcher.
    with EVENT_LOCK:
        for q in EVENT_QUEUES:
            q.put(message)


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
            "exports": exports_fingerprint()}


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
# A take edit's undo record in media/.trash/ (apply_take_edit): a cut or a pause.
EDIT_SIDECAR = re.compile(r"-before-(cut|pause|narrate|unnarrate)-\d{8}-\d{6}\.json$")
# A take's narrations (the Narrate tab), so each can be listed and removed at
# any time, not only while it is the take's last edit: the stretch it covers
# and, in media/.narrated/, the audio it replaced. A later cut or pause before
# one shifts it; one inside its stretch drops the row (the kept audio no longer
# fits), as does narrating over it again. The page lists them through /query.
NARRATIONS_SQL = """CREATE TABLE IF NOT EXISTS narrations (
  id INTEGER PRIMARY KEY,
  take_id INTEGER NOT NULL,
  t_in REAL NOT NULL,
  t_out REAL NOT NULL,
  audio TEXT NOT NULL,
  created_at TEXT NOT NULL
)"""
NARRATED = os.path.join(ROOT, ".narrated")
NARR_EDGE = 0.04  # one frame: an edit this close to a narration's end isn't inside it
TRASH = os.path.join(ROOT, ".trash")
EDIT_LOCK = threading.Lock()  # one undo, redo or history read at a time


# A take's edit history (the History tab): every edit leaves a sidecar in
# media/.trash/ (<stem>-before-<kind>-<when>.json, written by apply_take_edit)
# with the take's row, items and narrations as they were before it, and its
# files from before it beside it. The sidecars of one take file, in time
# order, are its stack. An undone edit keeps its sidecar, marked "undone",
# with the take as it was after the edit (files as <stem>-after-…, rows in
# the mark), so it can be redone. The edit in effect most recently is the
# cursor: the one Undo takes back. A new edit discards the undone ones.
def take_edits(file):
    # [(sidecar name, its contents)] for one take file, oldest first.
    stem, out = file[:-4], []
    try:
        names = os.listdir(TRASH)
    except OSError:
        names = []
    for n in sorted(names, key=lambda n: n[-20:]):
        m = EDIT_SIDECAR.search(n)
        if not m or n[:m.start()] != stem:
            continue
        try:
            with open(os.path.join(TRASH, n)) as f:
                out.append((n, json.load(f)))
        except (OSError, ValueError):
            continue
    return out


def edit_kind(name, saved):
    return saved.get("edit") or EDIT_SIDECAR.search(name).group(1)


def edit_what(name, saved):
    kind = edit_kind(name, saved)
    rec = saved.get(kind) or [0, 0]
    if kind == "unnarrate":
        return f"Removed the narration at {rec[0]:.2f}–{rec[1]:.2f} s"
    if kind == "narrate":
        return f"Narrated over {rec[0]:.2f}–{rec[1]:.2f} s"
    if kind == "pause":
        return f"Inserted a {rec[1]:g} s pause at {rec[0]:.2f} s"
    return f"Cut {rec[0]:.2f}–{rec[1]:.2f} s"


def file_stat(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def history_state(file):
    # (edits, cursor index or None, redo index or None, why not) for a take
    # file. The cursor is the newest edit not undone; it can be undone only
    # while the take's file is still the one that edit left (a file replaced
    # some other way, as remix_take.py does, ends that). The redo candidate
    # is the oldest undone edit, redoable while the file is the one its undo
    # restored.
    edits = take_edits(file)
    done = [i for i, (_, s) in enumerate(edits) if not s.get("undone")]
    undone = [i for i, (_, s) in enumerate(edits) if s.get("undone")]
    cursor, redo, why = (done[-1] if done else None), (undone[0] if undone else None), ""
    try:
        live = file_stat(os.path.join(ROOT, file))
    except OSError:
        return edits, None, None, "The take's file is missing."
    if cursor is not None and edits[cursor][1].get("after") != live:
        cursor, why = None, "This take's file has changed since its last edit, so that edit can't be undone."
    if redo is not None and edits[redo][1]["undone"].get("stat") != live:
        redo = None
    return edits, cursor, redo, why


def resync_history(file, was):
    # The take's file was rewritten without being an edit (Apply burns the
    # items in): where the history described the file as it was (`was`, its
    # size and mtime before), point it at the file as it is now.
    with EDIT_LOCK:
        edits = take_edits(file)
        done = [e for e in edits if not e[1].get("undone")]
        undone = [e for e in edits if e[1].get("undone")]
        try:
            live = file_stat(os.path.join(ROOT, file))
        except OSError:
            return
        for name, saved in (done[-1:] + undone[:1]):
            if saved.get("undone"):
                if saved["undone"].get("stat") != was:
                    continue
                saved["undone"]["stat"] = live
            else:
                if saved.get("after") != was:
                    continue
                saved["after"] = live
            with open(os.path.join(TRASH, name), "w") as f:
                json.dump(saved, f, indent=1)

# Which files in media/exports/ Studio wrote itself, so the lists can tell
# them from an editor's output even when the editor reuses the name (ScreenPal
# saves an edit under its input's name by default): {name: {kind: "export" |
# "leveled", size, mtime_ns}}. A file matches only if all three still agree;
# anything else there is an edited export.
STUDIO_FILES = os.path.join(ROOT, "exports", ".studio-files.json")
STUDIO_LOCK = threading.Lock()


def studio_files():
    try:
        with open(STUDIO_FILES) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_studio_files(files):
    tmp = STUDIO_FILES + ".tmp"
    with open(tmp, "w") as f:
        json.dump(files, f, indent=1)
    os.replace(tmp, STUDIO_FILES)


def record_studio_file(name, path, kind):
    # path may be the hidden .part file about to be renamed to name: a rename
    # keeps size and mtime, and recording first means the watcher never sees
    # the file under its final name unrecorded.
    st = os.stat(path)
    with STUDIO_LOCK:
        files = studio_files()
        files[name] = {"kind": kind, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        save_studio_files(files)


def forget_studio_file(name):
    with STUDIO_LOCK:
        files = studio_files()
        if files.pop(name, None) is not None:
            save_studio_files(files)


def studio_kind(name, st, files):
    e = files.get(name)
    if e and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns:
        return e.get("kind")
    return None


# A free-text note per listed file, saying what it is ("edit of the 22:09
# export, intro cut"), since names can't: {key: note}, key = the name in
# media/exports/.
NOTES = os.path.join(ROOT, "exports", ".notes.json")
NOTES_LOCK = threading.Lock()


# The mic record.sh uses (narrations record from it too), and the whisper
# model register.py uses.
NOTE_MIC = os.environ.get("MIC", "MacBook Air Microphone")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL",
                               os.path.expanduser("~/.local/share/whisper-models/ggml-small.en.bin"))
DICTATION_LOCK = threading.Lock()

# Dictated notes. The page does the dictating: Bram's script
# (/__shell/dictation.js, judell/bram#417) captures the mic in the browser,
# transcribes against a whisper-server and writes into the note field;
# studio-dictation.js is the bridge to it. This server keeps two things:
# - the claim on the mic ({"key": the row}), so a note, a take, a narration
#   and a voice test still exclude each other (claim_dictation);
# - which engine the page should use (dictation_engine): Bram's when it
#   answers, else one started here.
dictation = {"current": None}
BRAM_ENGINE_PORT = 18080  # Bram starts it on a first 🎤 click in its pane
WHISPER_PORT = 8767
whisper_server = {"proc": None}

# Narrating over a stretch of a take (/narrate/start, /go, /stop): the one
# narration in progress, guarded by DICTATION_LOCK since it owns the mic like
# a dictation does. Recorded with record.sh's native recorder, not ffmpeg's
# capture, which drops ~10% of samples (see voicetest.py).
narration = {"current": None}
NATIVE_SRC = os.path.join(HERE, "record_native.swift")
NATIVE_BIN = os.path.expanduser("~/.cache/bram-studio/record_native")
# A take's voice chain (render.py): the denoise, then highpass and compression.
NARRATE_AF = ("anlmdn=s=0.0005:p=0.002:r=0.006,highpass=f=80,"
              "acompressor=threshold=-24dB:ratio=3:attack=5:release=120")
NARRATE_FLOOR = -45.0  # LUFS: a recording quieter than this has no speech in it
NARRATE_GRACE = 30  # s past the clip's length before an unstopped narration is abandoned


# A recording's events, as the page sends them one by one (POST /record/event)
# into <session>/events.jsonl. The page also keeps them in one array that it
# rewrites on every event and posts at Stop; an item placed while recording
# was found with its annotAdd and annotRemove missing from that array
# (session 20261001-150809), so its span ran to the end of the take. At Stop
# the two are merged and compared (stop_recording). Events that arrive before
# record.sh has made its session directory wait in pending_events.
EVENTS_LOCK = threading.Lock()
pending_events = []


def log_events(events):
    with EVENTS_LOCK:
        pending_events.extend(events)
        try:
            session = open(os.path.join(ROOT, ".record-session")).read().strip()
        except OSError:
            return
        if pending_events and os.path.isdir(session):
            with open(os.path.join(session, "events.jsonl"), "a") as f:
                f.writelines(json.dumps(e) + "\n" for e in pending_events)
            pending_events.clear()


def event_key(e):
    return e.get("event"), e.get("wallMs"), e.get("cid")


def end_narration(n, keep=False):
    # Stop n's recorder (SIGTERM: the writer finalizes the WAV) and, unless
    # keep, remove its files.
    if n["proc"].poll() is None:
        n["proc"].terminate()
        try:
            n["proc"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            n["proc"].kill()
    n["out"].close()
    for path in (n["out"].name,) + (() if keep else (n["wav"],)):
        if os.path.exists(path):
            os.remove(path)


def watch_narration(n):
    # The page ends a narration (/narrate/stop). If it never does (reloaded,
    # closed), don't leave the mic open: abandon it, changing nothing.
    if n["done"].wait(n["end"] - n["start"] + NARRATE_GRACE):
        return
    with DICTATION_LOCK:
        if narration["current"] is not n:
            return
        narration["current"] = None
    end_narration(n)
    print(f"narrate: abandoned take {n['id']} (never stopped)", flush=True)


def engine_answers(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def dictating_where():
    # What a take, a narration or a voice test says when it refuses because a
    # note is being dictated. It names the page and the row: the dictation
    # may be open on a page you have left (2026-10-02: a take was refused and
    # the open note had to be hunted for).
    d = dictation["current"]
    where, _, name = (d["key"] if d else "").partition("/")
    if not name:
        return "a note is being dictated"
    return f"a note is being dictated on the {where.capitalize()} page ({name})"


def ensure_whisper_server():
    # Make sure an engine is on WHISPER_PORT. One already there is used as it
    # is, whoever started it: whisper-server shares its port, so starting
    # another on each restart of this server left six of them running
    # (found 2026-10-02).
    if engine_answers(WHISPER_PORT):
        return True
    try:
        whisper_server["proc"] = subprocess.Popen(
            ["whisper-server", "-m", WHISPER_MODEL, "--host", "127.0.0.1", "--port", str(WHISPER_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    for _ in range(100):  # the model loads in a second or two
        if engine_answers(WHISPER_PORT):
            return True
        time.sleep(0.1)
    return False


def note_key(name, where):
    # where is always "exports" now (the Desktop is no longer listed).
    return name


def export_notes():
    try:
        with open(NOTES) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def set_export_note(key, note):
    # An empty note removes the entry.
    note = (note or "").strip()
    with NOTES_LOCK:
        notes = export_notes()
        if note:
            notes[key] = note
        elif notes.pop(key, None) is None:
            return
        os.makedirs(os.path.dirname(NOTES), exist_ok=True)
        with open(NOTES + ".tmp", "w") as f:
            json.dump(notes, f, indent=1)
        os.replace(NOTES + ".tmp", NOTES)


def backfill_studio_files():
    # Once, for files written before the manifest existed: an Export-named file
    # modified within 10 minutes of the time in its name is Export's own (an
    # editor saving under that name later is off by more); a leveled- file with
    # its loudness report is Level's. After this only the manifest decides.
    outdir = os.path.join(ROOT, "exports")
    with STUDIO_LOCK:
        files = studio_files()
        changed = False
        for name in os.listdir(outdir) if os.path.isdir(outdir) else []:
            path = os.path.join(outdir, name)
            if name in files or name.startswith(".") or not os.path.isfile(path):
                continue
            st = os.stat(path)
            kind = None
            if EXPORT_NAME.match(name):
                stamp = time.mktime(time.strptime(name[:15], "%Y%m%d-%H%M%S"))
                if abs(st.st_mtime - stamp) <= 600:
                    kind = "export"
            elif name.startswith("leveled-") and os.path.exists(os.path.join(outdir, f".{name}.json")):
                kind = "leveled"
            if kind:
                files[name] = {"kind": kind, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                changed = True
                print(f"backfill_studio_files: {name} -> {kind}", flush=True)
        if changed:
            save_studio_files(files)


def export_entry(path, url, where, from_name=False):
    # {name, url, created, seconds, where} for one listed file. created comes
    # from the name's leading <YYYYMMDD-HHMMSS> stamp for Export's own files
    # (from_name), else the file's mtime (an editor may reuse an export's name);
    # lengths are probed once per file version.
    # A file modified in the last WRITING_S seconds is still being written by
    # whatever editor saved it: report its size, and don't probe (or cache) a
    # length that isn't final.
    name = os.path.basename(path)
    st = os.stat(path)
    key = (path, st.st_mtime)
    m = from_name and re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
    created = (f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]}" if m
               else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(key[1])))
    entry = {"name": name, "url": url, "created": created, "where": where,
             "writing": is_writing(st), "bytes": st.st_size, "seconds": None}
    if not entry["writing"]:
        if key not in EXPORT_SECONDS:
            EXPORT_SECONDS[key] = round(float(probe(path, "format=duration") or 0), 1)
        entry["seconds"] = EXPORT_SECONDS[key]
    return entry


WRITING_S = 3  # seconds since the last write before a file counts as finished


def is_writing(st):
    return time.time() - st.st_mtime < WRITING_S


def exports_fingerprint():
    # Folder mtimes catch files appearing, renamed or removed; (name, size,
    # writing) per listed file catches one growing, and the moment it stops.
    def files(folder, keep):
        try:
            names = sorted(n for n in os.listdir(folder) if keep(n))
        except OSError:
            return ()
        out = []
        for n in names:
            try:
                st = os.stat(os.path.join(folder, n))
            except OSError:
                continue
            out.append((n, st.st_size, is_writing(st)))
        return tuple(out)
    exports = os.path.join(ROOT, "exports")
    return files(exports, lambda n: n.endswith(".mp4") and not n.startswith("."))


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
    # The two lists of media/exports/, each newest first:
    #   leveled: files Level wrote (recorded in STUDIO_FILES);
    #   exports: every other .mp4 there. One that isn't exactly what Export
    #            wrote (saved over in place by an editor, or saved beside it
    #            under any name) is marked edited, and dated by its mtime.
    # Hidden names (.<name>.part.mp4 while being written) are skipped.
    outdir = os.path.join(ROOT, "exports")
    lists = {"exports": [], "leveled": []}
    files = studio_files()
    for name in os.listdir(outdir) if os.path.isdir(outdir) else []:
        path = os.path.join(outdir, name)
        if name.startswith(".") or not name.endswith(".mp4") or not os.path.isfile(path):
            continue
        kind = studio_kind(name, os.stat(path), files)
        entry = export_entry(path, f"http://127.0.0.1:{PORT}/exports/{name}", "exports",
                             from_name=kind == "export")
        if kind == "leveled":
            entry["report"] = level_report(outdir, name)
            lists["leveled"].append(entry)
        else:
            entry["edited"] = kind != "export"
            lists["exports"].append(entry)
    notes = export_notes()
    for entries in lists.values():
        for e in entries:
            e["note"] = notes.get(note_key(e["name"], e["where"]), "")
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
        if self.path == "/dictation/status":
            return self.dictation_status()
        if self.path == "/dictation/engine":
            return self.dictation_engine()
        if self.path.startswith("/takes/edits"):
            return self.list_edits()
        if self.path == "/narrate/status":
            with DICTATION_LOCK:
                n = narration["current"]
            return self.send_json(200, {"id": n["id"] if n else None})
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
                    self.wfile.write(f"data: {json.dumps(q.get(timeout=15))}\n\n".encode())
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
        if self.path == "/record/event":
            return self.record_event()
        if self.path == "/voicetest":
            return self.run_voicetest()
        if self.path == "/delete":
            return self.delete_take()
        if self.path == "/undelete":
            return self.undelete_take()
        if self.path == "/takes/cut":
            return self.cut_take()
        if self.path == "/takes/pause":
            return self.pause_take()
        if self.path == "/takes/undo":
            return self.undo_take()
        if self.path == "/takes/redo":
            return self.redo_take()
        if self.path == "/narrate/start":
            return self.narrate_start()
        if self.path == "/narrate/go":
            return self.narrate_go()
        if self.path == "/narrate/stop":
            return self.narrate_stop()
        if self.path == "/narrate/remove":
            return self.narrate_remove()
        if self.path == "/reorder":
            return self.reorder_takes()
        if self.path == "/export":
            return self.export_takes()
        if self.path == "/exports/delete":
            return self.delete_export()
        if self.path == "/exports/level":
            return self.level_export()
        if self.path == "/exports/note":
            return self.save_export_note()
        if self.path == "/dictation/start":
            return self.claim_dictation()
        if self.path == "/dictation/stop":
            return self.release_dictation()
        if self.path == "/dictation/trace":
            return self.dictation_trace()
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
        # .mp4 names in media/exports/ only (where is always "exports").
        if "/" in name or name.startswith(".") or not name.endswith(".mp4") or where == "desktop":
            return None
        path = os.path.join(ROOT, "exports", name)
        return path if os.path.isfile(path) else None

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
        forget_studio_file(name)
        set_export_note(note_key(name, body.get("where")), "")
        report = os.path.join(os.path.dirname(src), f".{name}.json")
        if os.path.exists(report):
            shutil.move(report, os.path.join(trash, f".{os.path.basename(dest)}.json"))
        self.send_json(200, {"deleted": name, "trashed": f".trash/{os.path.basename(dest)}"})

    def claim_dictation(self):
        # {key}: the page is about to dictate into that row's note. One at a
        # time, and never while a take, a narration or a voice test has the
        # mic. The same row claiming again is fine (the page re-claims when
        # its event stream reconnects mid-dictation).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        key = str(body.get("key", ""))
        with DICTATION_LOCK:
            if self.recording():
                return self.send_json(409, {"error": "a take is recording"})
            if narration["current"]:
                return self.send_json(409, {"error": "a narration is recording"})
            if testing.locked():
                return self.send_json(409, {"error": "a voice test is running"})
            d = dictation["current"]
            if d and d["key"] != key:
                return self.send_json(409, {"error": "already dictating"})
            dictation["current"] = {"key": key}
        if not d:
            print(f"dictation: claim {key}", flush=True)
        self.send_json(200, {"dictating": key})

    def release_dictation(self):
        # The page stopped dictating, loaded with nothing dictating, or is
        # going away. Always answers; releasing nothing is not an error.
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with DICTATION_LOCK:
            d, dictation["current"] = dictation["current"], None
        if d:
            print(f"dictation: release {d['key']}", flush=True)
        self.send_json(200, {"released": d["key"] if d else None})

    def dictation_status(self):
        with DICTATION_LOCK:
            d = dictation["current"]
        self.send_json(200, {"key": d["key"] if d else None})

    def dictation_engine(self):
        # Which whisper-server the page should use: Bram's if it answers (one
        # model in memory for both apps), else one on WHISPER_PORT, started
        # here if need be. Bram's script can't start an engine itself; when
        # Bram gets a route for that (judell/bram#417) this fallback can go.
        if engine_answers(BRAM_ENGINE_PORT):
            return self.send_json(200, {"host": f"http://127.0.0.1:{BRAM_ENGINE_PORT}"})
        if ensure_whisper_server():
            return self.send_json(200, {"host": f"http://127.0.0.1:{WHISPER_PORT}"})
        self.send_json(503, {"error": "no speech engine: Bram's isn't running and whisper-server didn't start"})

    def dictation_trace(self):
        # {key, lines: [{at, stage, fields}]}: the script's own trace lines,
        # batched by the bridge, kept here so a dictation that went wrong
        # leaves evidence. Never an error for the page to show.
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            for ln in body.get("lines", [])[:200]:
                at = time.strftime("%H:%M:%S", time.localtime(ln.get("at", 0) / 1000))
                print(f"dictation: {at} {ln.get('stage')} {json.dumps(ln.get('fields') or {})}", flush=True)
        except (ValueError, TypeError, AttributeError):
            pass
        self.send_json(200, {})

    def save_export_note(self):
        # {name, where, note} for a listed file; an empty note removes it.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name, where = str(body.get("name", "")), body.get("where")
        if not self.listed_export(name, where):
            return self.send_json(404, {"error": f"no export named {name!r}"})
        set_export_note(note_key(name, where), str(body.get("note", ""))[:500])
        self.send_json(200, {"name": name, "note": export_notes().get(note_key(name, where), "")})

    def level_export(self):
        # Run level_edit.py on an export or an edited file (any listed .mp4
        # that isn't already leveled) into media/exports/leveled-<name>.mp4, an
        # edited- prefix dropped (written hidden, then renamed, like Export),
        # keeping its before/after loudness in .leveled-<name>.mp4.json for the
        # list. Leveling again replaces the earlier result.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = str(body.get("name", ""))
        src = self.listed_export(name, body.get("where"))
        if not src or studio_kind(name, os.stat(src), studio_files()) == "leveled":
            return self.send_json(404, {"error": f"no export to level named {name!r}"})
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
        record_studio_file(leveled, part, "leveled")
        os.replace(part, os.path.join(outdir, leveled))
        note = export_notes().get(note_key(name, body.get("where")))
        if note:
            set_export_note(leveled, f"{note} (leveled)")
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
            record_studio_file(name, out, "export")
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
        # A callout may have no text yet, as one placed while recording may:
        # the editor shows a placeholder (overlay.sprite) and Apply skips it.
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
            # id: the saved item this is, echoed so the page can tell a sprite
            # that has just loaded from the previously selected item's.
            self.send_json(200, {**self.sprite_file(db, take_id, item), "id": body.get("id")})
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
        # Apply rewrites the take's file without being an edit: keep the
        # take's edit history pointing at it (resync_history).
        row = sqlite3.connect(os.path.join(HERE, "studio.db")).execute(
            "SELECT file FROM takes WHERE id = ?", (body["id"],)).fetchone()
        try:
            was = file_stat(os.path.join(ROOT, row[0])) if row else None
        except OSError:
            was = None
        try:
            r = subprocess.run(["python3", os.path.join(HERE, "overlay.py"), "render", str(body["id"])],
                               cwd=HERE, capture_output=True, text=True)
        finally:
            overlaying.release()
        if r.returncode:
            return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["render failed"])[-1]})
        if was:
            resync_history(row[0], was)
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
                     "SELECT * FROM overlays WHERE take_id = ? ORDER BY id", (row["id"],))],
                 "narrations": [dict(r) for r in db.execute(
                     "SELECT * FROM narrations WHERE take_id = ? ORDER BY id", (row["id"],))]}
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
        db.execute("DELETE FROM narrations WHERE take_id = ?", (row["id"],))
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
        for n in saved.get("narrations", []):
            n = {k: v for k, v in n.items() if k != "id"}
            n["take_id"] = take_id
            db.execute(f"INSERT INTO narrations ({', '.join(n)}) VALUES ({', '.join('?' * len(n))})", list(n.values()))
        db.commit()
        os.remove(path)
        self.send_json(200, {"restored": take_id, "name": take["name"], "overlays": len(saved["overlays"])})

    def cut_take(self):
        # {id, start, end}: remove that stretch from a take. The same cut is
        # made to the take's MP4 and to its clean copy (so burned-in callouts
        # need no re-apply), re-encoded with the takes' own settings so Export
        # can still join by stream copy. Overlays after the cut shift earlier,
        # ones overlapping it are clipped, ones inside it go. The originals and
        # a sidecar (the take's row, its overlays) go to media/.trash/ first,
        # for Undo (undo_take).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM takes WHERE id = ?", (body.get("id"),)).fetchone()
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        src = os.path.join(ROOT, row["file"])
        if not os.path.isfile(src):
            return self.send_json(404, {"error": f"{row['file']} is missing"})
        try:
            start, end = float(body.get("start")), float(body.get("end"))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "start and end must be numbers"})
        dur = float(probe(src, "format=duration") or 0)
        start, end = max(0.0, start), min(dur, end)
        if end - start < 0.1:
            return self.send_json(400, {"error": "the cut must be at least 0.1 s"})
        if dur - (end - start) < 0.5:
            return self.send_json(400, {"error": "the cut would leave less than 0.5 s of the take"})
        segs = [s for s in ((0.0, start), (end, dur)) if s[1] - s[0] > 0.02]
        chain = ""
        for i, (a, b) in enumerate(segs):
            fades = ([f"afade=t=out:st={b - a - 0.01:.3f}:d=0.01"] if i < len(segs) - 1 else []) + \
                    (["afade=t=in:d=0.01"] if i > 0 else [])
            chain += (f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
                      f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
                      f"{''.join(',' + x for x in fades)}[a{i}];")
        chain += "".join(f"[v{i}][a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=1[v][a]"
        cut = end - start

        def retime(t_in, t_out):
            if t_out <= start:
                return t_in, t_out
            if t_in >= end:
                return t_in - cut, t_out - cut
            # overlaps the cut: keep what's left of it on either side
            return min(t_in, start), (t_out - cut if t_out > end else start)

        def narr(t_in, t_out):
            # A cut through a narration takes its row; one before it moves it.
            if start < t_out - NARR_EDGE and end > t_in + NARR_EDGE:
                return None
            return (max(start, t_in - cut), t_out - cut) if t_in >= end - NARR_EDGE else (t_in, t_out)

        done = self.apply_take_edit(db, row, src, chain, "cut", [round(start, 3), round(end, 3)], retime, narr=narr)
        if done:
            self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "cut", "start": round(start, 2),
                                 "end": round(end, 2), "duration": done[0], "undo": done[1]})

    def pause_take(self):
        # {id, at, seconds}: hold the frame at `at` for that long, with silence.
        # Made like a cut (both files, same encoding, same undo). Overlays
        # starting at or after `at` move later; one on the held frame is
        # extended, so it stays up through the pause, as it does in the picture.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM takes WHERE id = ?", (body.get("id"),)).fetchone()
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        src = os.path.join(ROOT, row["file"])
        if not os.path.isfile(src):
            return self.send_json(404, {"error": f"{row['file']} is missing"})
        try:
            at, secs = float(body.get("at")), float(body.get("seconds"))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "at and seconds must be numbers"})
        dur = float(probe(src, "format=duration") or 0)
        at = min(max(0.0, at), dur)
        if not 0.1 <= secs <= 60:
            return self.send_json(400, {"error": "the pause must be between 0.1 and 60 s"})
        fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
        quiet = f"anullsrc=r=48000:cl=stereo,atrim=duration={secs:.3f},{fmt}[as];"
        if at < 0.02:  # hold the first frame
            chain = (f"[0:v]tpad=start_mode=clone:start_duration={secs:.3f}[v];{quiet}"
                     f"[0:a]{fmt},afade=t=in:d=0.01[a1];[as][a1]concat=n=2:v=0:a=1[a]")
        elif at > dur - 0.02:  # hold the last frame
            chain = (f"[0:v]tpad=stop_mode=clone:stop_duration={secs:.3f}[v];{quiet}"
                     f"[0:a]{fmt},afade=t=out:st={max(0, dur - 0.01):.3f}:d=0.01[a0];[a0][as]concat=n=2:v=0:a=1[a]")
        else:
            chain = (f"[0:v]trim=start=0:end={at:.3f},setpts=PTS-STARTPTS,"
                     f"tpad=stop_mode=clone:stop_duration={secs:.3f}[v0];"
                     f"[0:v]trim=start={at:.3f},setpts=PTS-STARTPTS[v1];[v0][v1]concat=n=2:v=1:a=0[v];"
                     f"[0:a]atrim=start=0:end={at:.3f},asetpts=PTS-STARTPTS,{fmt},"
                     f"afade=t=out:st={max(0, at - 0.01):.3f}:d=0.01[a0];{quiet}"
                     f"[0:a]atrim=start={at:.3f},asetpts=PTS-STARTPTS,{fmt},afade=t=in:d=0.01[a1];"
                     "[a0][as][a1]concat=n=3:v=0:a=1[a]")

        # The frame the pause holds: the last one before `at` (the first frame
        # when the pause is at the very start). An overlay on that frame is in
        # the held picture, so it must last through the pause: that includes
        # one ending exactly at `at` (a shape running to the end of the take,
        # or clipped there by a cut), which `t_out > at` used to miss, leaving
        # the editor showing a bare frame while the burned take kept the shape.
        held = 0.0 if at < 0.02 else at - 0.04
        eps = 0.005

        def retime(t_in, t_out):
            if t_in <= held + eps and t_out > held + eps:
                return t_in, t_out + secs
            if t_in >= at - eps:
                return t_in + secs, t_out + secs
            return t_in, t_out

        def narr(t_in, t_out):
            # A pause inside a narration takes its row; one before it moves it.
            if t_in + NARR_EDGE < at < t_out - NARR_EDGE:
                return None
            return (t_in + secs, t_out + secs) if t_in >= at - NARR_EDGE else (t_in, t_out)

        done = self.apply_take_edit(db, row, src, chain, "pause", [round(at, 3), round(secs, 3)], retime, narr=narr)
        if done:
            self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "pause", "at": round(at, 2),
                                 "seconds": round(secs, 2), "duration": done[0], "undo": done[1]})

    def narrate_start(self):
        # {id, start, end}: open the mic to narrate over that stretch of a take.
        # Answers once samples are flowing; the page then starts the (muted)
        # player and posts /narrate/go, and /narrate/stop when the clip ends.
        # One at a time, and never while a take records, a note is dictated or
        # a voice test runs.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM takes WHERE id = ?", (body.get("id"),)).fetchone()
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        src = os.path.join(ROOT, row["file"])
        if not os.path.isfile(src):
            return self.send_json(404, {"error": f"{row['file']} is missing"})
        try:
            start, end = float(body.get("start")), float(body.get("end"))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "start and end must be numbers"})
        dur = float(probe(src, "format=duration") or 0)
        start, end = max(0.0, start), min(dur, end)
        if end - start < 0.5:
            return self.send_json(400, {"error": "the clip to narrate over must be at least 0.5 s"})
        if not os.path.exists(NATIVE_BIN) or os.path.getmtime(NATIVE_BIN) < os.path.getmtime(NATIVE_SRC):
            os.makedirs(os.path.dirname(NATIVE_BIN), exist_ok=True)
            r = subprocess.run(["swiftc", "-O", "-o", NATIVE_BIN, NATIVE_SRC], capture_output=True, text=True)
            if r.returncode:
                return self.send_json(500, {"error": "couldn't build the recorder (record_native.swift)"})
        with DICTATION_LOCK:
            if self.recording() or testing.locked():
                return self.send_json(409, {"error": "the mic is busy (recording or testing)"})
            if dictation["current"]:
                return self.send_json(409, {"error": dictating_where()})
            if narration["current"]:
                return self.send_json(409, {"error": "already narrating"})
            base = os.path.join(tempfile.gettempdir(), f"studio-narrate-{os.getpid()}")
            out = open(base + ".out", "w+")
            proc = subprocess.Popen([NATIVE_BIN, NOTE_MIC, base + ".wav", "0"],
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE)
            n = {"id": row["id"], "start": start, "end": end, "proc": proc, "wav": base + ".wav", "out": out,
                 "t0": None, "go": None, "done": threading.Event()}
            # The recorder prints "started <epoch>" when samples begin: the
            # narration's time zero, as record.sh's t0 is a take's.
            deadline = time.time() + 5
            while time.time() < deadline and proc.poll() is None and n["t0"] is None:
                with open(out.name) as f:
                    m = re.search(r"^started (\S+)", f.read(), re.M)
                if m:
                    n["t0"] = float(m.group(1))
                else:
                    time.sleep(0.05)
            if n["t0"] is None:
                if proc.poll() is None:
                    proc.kill()
                err = (proc.stderr.read().decode(errors="replace").strip().splitlines() or ["no audio"])[-1]
                end_narration(n)
                print(f"narrate: start failed: {err}", flush=True)
                return self.send_json(500, {"error": f"couldn't open the microphone: {err}"})
            narration["current"] = n
        threading.Thread(target=watch_narration, args=(n,), daemon=True).start()
        print(f"narrate: start take {n['id']} {start:.2f}-{end:.2f}s", flush=True)
        self.send_json(200, {"narrating": n["id"], "start": round(start, 2), "end": round(end, 2)})

    def narrate_go(self):
        # The page has just started playing the clip: what's recorded from now
        # on belongs at the clip's start.
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with DICTATION_LOCK:
            n = narration["current"]
            if n and n["go"] is None:
                n["go"] = time.time()
        if not n:
            return self.send_json(409, {"error": "not narrating"})
        self.send_json(200, {"go": round(n["go"] - n["t0"], 3)})

    def narrate_stop(self):
        # Stop the mic and replace the take's audio over the stretch with what
        # was said since /narrate/go: denoised, high-passed and compressed as a
        # take's voice is, then one gain to -16 LUFS (no loudnorm: on a few
        # seconds it falls back to its dynamic mode) and a -1.5 dB limiter,
        # with 10 ms fades at the joins. Stopped early, the rest of the stretch
        # is silent. The picture is stream-copied. Nothing said, or stopped
        # before playback began: the take is left alone. {discard: true} drops
        # the recording. Always clears the state and always answers.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        stopped = time.time()
        with DICTATION_LOCK:
            n, narration["current"] = narration["current"], None
        if not n:
            return self.send_json(200, {"idle": True})
        n["done"].set()
        voice = n["wav"][:-4] + "-voice.wav"
        try:
            end_narration(n, keep=True)
            if body.get("discard") or n["go"] is None:
                print(f"narrate: discarded take {n['id']}", flush=True)
                return self.send_json(200, {"discarded": True})
            clip = n["end"] - n["start"]
            offset, said = max(0.0, n["go"] - n["t0"]), min(clip, stopped - n["go"])
            if said < 0.3:
                return self.send_json(400, {"error": "stopped too soon: nothing was recorded"})
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{offset:.3f}", "-t", f"{said:.3f}",
                                "-i", n["wav"], "-af", NARRATE_AF, "-ar", "48000", "-ac", "1", voice],
                               capture_output=True, text=True)
            if r.returncode:
                return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["narration failed"])[-1]})
            m = subprocess.run(["ffmpeg", "-v", "info", "-i", voice, "-af", "loudnorm=print_format=json",
                                "-f", "null", "-"], capture_output=True, text=True).stderr
            ln = json.loads(m[m.rindex("{"):m.rindex("}") + 1])
            loud = float("-inf") if "inf" in ln["input_i"] else float(ln["input_i"])
            print(f"narrate: take {n['id']} offset={offset:.3f}s said={said:.2f}s of {clip:.2f}s "
                  f"measured {ln['input_i']} LUFS, peak {ln['input_tp']} dBTP", flush=True)
            if loud < NARRATE_FLOOR:
                return self.send_json(400, {"error": "no speech was heard, so the take is unchanged"})
            gain = -16.0 - loud
            db = sqlite3.connect(os.path.join(HERE, "studio.db"))
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM takes WHERE id = ?", (n["id"],)).fetchone()
            src = os.path.join(ROOT, row["file"]) if row else ""
            if not os.path.isfile(src):
                return self.send_json(409, {"error": "the take is gone"})
            dur = float(probe(src, "format=duration") or 0)
            start, end = n["start"], n["end"]
            chain = self.replace_audio_chain(
                start, end, dur, f"volume={gain:.2f}dB,alimiter=limit=0.841:attack=5:release=80:level=false,"
                                 f"afade=t=in:d=0.01,afade=t=out:st={said - 0.01:.3f}:d=0.01")
            # Keep the audio being replaced, so this narration can be removed
            # later (/narrate/remove) whatever else has been done to the take.
            os.makedirs(NARRATED, exist_ok=True)
            kept = f"{row['file'][:-4]}-{time.strftime('%Y%m%d-%H%M%S')}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-af",
                            f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS",
                            "-ar", "48000", "-ac", "2", os.path.join(NARRATED, kept)], check=True)
            # Narrating over an earlier narration's stretch takes that one's
            # row: its kept audio would wipe this narration too.
            done = self.apply_take_edit(
                db, row, src, chain, "narrate", [round(start, 3), round(end, 3)],
                lambda t_in, t_out: (t_in, t_out), extra=[voice], copy_video=True,
                narr=lambda a, b: None if start < b - NARR_EDGE and end > a + NARR_EDGE else (a, b))
            if not done:
                os.remove(os.path.join(NARRATED, kept))
            if done:
                db.execute("INSERT INTO narrations (take_id, t_in, t_out, audio, created_at) VALUES (?, ?, ?, ?, ?)",
                           (row["id"], round(start, 3), round(end, 3), kept, time.strftime("%Y-%m-%d %H:%M:%S")))
                db.commit()
                self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "narrate",
                                     "start": round(start, 2), "end": round(end, 2), "said": round(said, 2),
                                     "measured": loud, "gain": round(gain, 1), "duration": done[0], "undo": done[1]})
        except Exception:
            print(f"narrate: stop failed\n{traceback.format_exc()}", flush=True)
            self.send_json(500, {"error": "narration failed (see serve_media.log)"})
        finally:
            for path in (n["wav"], voice):
                if os.path.exists(path):
                    os.remove(path)

    @staticmethod
    def replace_audio_chain(start, end, dur, mid):
        # The filter chain (ending in [a]) that replaces a take's audio from
        # start to end with input 1 run through `mid`, padded or trimmed to
        # the stretch's length, with 10 ms fades where the take's own audio
        # stops and resumes.
        fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
        clip, parts, chain = end - start, [], ""
        if start > 0.02:
            chain += (f"[0:a]atrim=start=0:end={start:.3f},asetpts=PTS-STARTPTS,{fmt},"
                      f"afade=t=out:st={start - 0.01:.3f}:d=0.01[a0];")
            parts.append("[a0]")
        chain += f"[1:a]{mid},{fmt},apad=whole_dur={clip:.3f},atrim=duration={clip:.3f}[a1];"
        parts.append("[a1]")
        if end < dur - 0.02:
            chain += f"[0:a]atrim=start={end:.3f},asetpts=PTS-STARTPTS,{fmt},afade=t=in:d=0.01[a2];"
            parts.append("[a2]")
        return chain + f"{''.join(parts)}concat=n={len(parts)}:v=0:a=1[a]"

    def narrate_remove(self):
        # {id}: take a narration back out: the audio it replaced
        # (media/.narrated/) goes back over its stretch, in the take and its
        # clean copy, the picture stream-copied. An edit like the others, so
        # the Undo line offers to put the narration back.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        db.row_factory = sqlite3.Row
        n = db.execute("SELECT * FROM narrations WHERE id = ?", (body.get("id"),)).fetchone()
        if not n:
            return self.send_json(404, {"error": f"no narration with id {body.get('id')}"})
        row = db.execute("SELECT * FROM takes WHERE id = ?", (n["take_id"],)).fetchone()
        src = os.path.join(ROOT, row["file"]) if row else ""
        kept = os.path.join(NARRATED, n["audio"])
        if not os.path.isfile(src):
            return self.send_json(409, {"error": "the take is gone"})
        if not os.path.isfile(kept):
            return self.send_json(409, {"error": f"the audio this narration replaced is missing ({n['audio']})"})
        with DICTATION_LOCK:
            if narration["current"]:
                return self.send_json(409, {"error": "a narration is recording"})
        dur = float(probe(src, "format=duration") or 0)
        start, end = n["t_in"], min(dur, n["t_out"])
        chain = self.replace_audio_chain(
            start, end, dur, f"afade=t=in:d=0.01,afade=t=out:st={max(0, end - start - 0.01):.3f}:d=0.01")
        done = self.apply_take_edit(db, row, src, chain, "unnarrate", [round(start, 3), round(end, 3)],
                                    lambda t_in, t_out: (t_in, t_out), extra=[kept], copy_video=True)
        if done:
            db.execute("DELETE FROM narrations WHERE id = ?", (n["id"],))
            db.commit()
            self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "unnarrate", "start": round(start, 2),
                                 "end": round(end, 2), "duration": done[0], "undo": done[1]})

    def apply_take_edit(self, db, row, src, chain, kind, record, retime, extra=(), copy_video=False, narr=None):
        # The part a cut, a pause, a narration and its removal share. Runs the ffmpeg filter
        # `chain` (ending in [v] and [a]) on the take's MP4 and its clean copy, into
        # hidden temp files first, so a failure changes nothing. `extra` are
        # further input files (inputs 1…); with copy_video the chain ends in
        # [a] only and the picture is stream-copied, not re-encoded. Moves the
        # originals to media/.trash/ with a sidecar (<stem>-before-<kind>-<when>
        # .json: the take's row, its overlays, `record`), swaps the new files
        # in, retimes the overlays with retime(t_in, t_out), and updates the
        # take's row. Returns (new duration, sidecar name), or None after
        # sending an error.
        clean = os.path.join(ROOT, ".clean", row["file"])
        targets = [src] + ([clean] if os.path.isfile(clean) else [])
        tmps = []
        for path in targets:
            tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)[:-4]}.{kind}.mp4")
            inputs = [a for p in (path, *extra) for a in ("-i", p)]
            video = ["-map", "0:v", "-c:v", "copy"] if copy_video else ["-map", "[v]", *overlay.ENC]
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", chain,
                                *video, "-map", "[a]",
                                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", tmp],
                               capture_output=True, text=True)
            tmps.append(tmp)
            if r.returncode:
                for t in tmps:
                    if os.path.exists(t):
                        os.remove(t)
                self.send_json(500, {"error": (r.stderr.strip().splitlines() or [f"{kind} failed"])[-1]})
                return None
        trash = os.path.join(ROOT, ".trash")
        os.makedirs(trash, exist_ok=True)
        # A new edit ends the chance to redo: the undone edits above the
        # cursor go, with the files kept for redoing them.
        with EDIT_LOCK:
            for name, old in take_edits(row["file"]):
                if old.get("undone"):
                    for f in list(old["undone"].get("files", {}).values()) + [name]:
                        if os.path.exists(os.path.join(trash, f)):
                            os.remove(os.path.join(trash, f))
        stem, when = row["file"][:-4], time.strftime("%Y%m%d-%H%M%S")
        saved = {"take": dict(row), "edit": kind, kind: record, "files": {},
                 "overlays": [dict(r) for r in db.execute(
                     "SELECT * FROM overlays WHERE take_id = ? ORDER BY id", (row["id"],))],
                 "narrations": [dict(r) for r in db.execute(
                     "SELECT * FROM narrations WHERE take_id = ? ORDER BY id", (row["id"],))]}
        was_applied =row["overlay_applied"] == db.execute(overlay.APPLIED_SQL, (row["id"],)).fetchone()[0]
        for path, tmp in zip(targets, tmps):
            which = "clean-" if path == clean else ""
            trashed = f"{stem}-{which}before-{kind}-{when}.mp4"
            shutil.move(path, os.path.join(trash, trashed))
            os.replace(tmp, path)
            saved["files"][os.path.relpath(path, ROOT)] = trashed
        for o in saved["overlays"]:
            new = retime(o["t_in"], o["t_out"])
            if new == (o["t_in"], o["t_out"]):
                continue
            if new[1] - new[0] < 0.05:
                db.execute("DELETE FROM overlays WHERE id = ?", (o["id"],))
            else:
                db.execute("UPDATE overlays SET t_in = ?, t_out = ? WHERE id = ?",
                           (round(new[0], 3), round(new[1], 3), o["id"]))
        # The take's narrations: narr(t_in, t_out) gives one's new stretch, or
        # None when this edit changed what is inside it, which takes its row
        # (its kept audio no longer fits). The sidecar has them all for undo.
        for n in saved["narrations"]:
            new = narr(n["t_in"], n["t_out"]) if narr else (n["t_in"], n["t_out"])
            if new is None:
                db.execute("DELETE FROM narrations WHERE id = ?", (n["id"],))
            elif new != (n["t_in"], n["t_out"]):
                db.execute("UPDATE narrations SET t_in = ?, t_out = ? WHERE id = ?",
                           (round(new[0], 3), round(new[1], 3), n["id"]))
        new_dur = round(float(probe(src, "format=duration") or 0), 1)
        url = row["url"].split("?")[0] + f"?v={int(time.time())}"
        db.execute("UPDATE takes SET duration_s = ?, url = ? WHERE id = ?", (new_dur, url, row["id"]))
        if was_applied:
            db.execute(f"UPDATE takes SET overlay_applied = ({overlay.APPLIED_SQL}) WHERE id = ?",
                       (row["id"], row["id"]))
        db.commit()
        st = os.stat(src)
        saved["after"] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        undo = f"{stem}-before-{kind}-{when}.json"
        with open(os.path.join(trash, undo), "w") as f:
            json.dump(saved, f, indent=1)
        # The take's separate tracks in work/takes/ aren't edited: note it in
        # the take's JSON so remix_take.py refuses rather than mis-mixes.
        self.mark_take_cut(row["file"], {kind: record})
        return new_dur, undo

    @staticmethod
    def mark_take_cut(file, cut):
        # cut = {"cut": [start, end]} or {"pause": [at, seconds]} to record an
        # edit; None to take the last one back.
        meta_path = os.path.join(HERE, "work", "takes", file.replace("-raw.mp4", ".json"))
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            cuts = meta.get("cutsAfterRender", [])
            cuts = cuts + [cut] if cut else cuts[:-1]
            meta["cutsAfterRender"] = cuts
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=1)
        except (OSError, ValueError) as e:
            print(f"mark_take_cut: {e}", flush=True)

    def take_row(self, take_id):
        db = sqlite3.connect(os.path.join(HERE, "studio.db"))
        db.row_factory = sqlite3.Row
        return db, db.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone()

    def list_edits(self):
        # GET /takes/edits?id=<take>: the take's edit history for the History
        # tab, newest first: {edits: [{name, kind, what, when, undone,
        # current}], canUndo, canRedo, note}. `current` marks the edit Undo
        # would take back; `note` says why an edit can't be undone, when the
        # take's file no longer matches its history.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            take_id = int(query.get("id", [""])[0])
        except ValueError:
            return self.send_json(200, {"edits": [], "canUndo": False, "canRedo": False, "note": ""})
        _, row = self.take_row(take_id)
        if not row:
            return self.send_json(200, {"edits": [], "canUndo": False, "canRedo": False, "note": ""})
        with EDIT_LOCK:
            edits, cursor, redo, why = history_state(row["file"])
        out = []
        for i, (name, saved) in enumerate(edits):
            when = time.strptime(name[-20:-5], "%Y%m%d-%H%M%S")
            out.append({"name": name, "kind": edit_kind(name, saved), "what": edit_what(name, saved),
                        "when": time.strftime("%b %-d, %H:%M:%S", when), "undone": bool(saved.get("undone")),
                        "current": i == cursor})
        self.send_json(200, {"edits": out[::-1], "canUndo": cursor is not None, "canRedo": redo is not None,
                             "note": why})

    @staticmethod
    def restore_rows(db, take_id, snap):
        # Put a take's row, items and narrations back as a snapshot has them.
        take = snap["take"]
        url = take["url"].split("?")[0] + f"?v={int(time.time())}"
        db.execute("UPDATE takes SET duration_s = ?, url = ?, overlay_applied = ? WHERE id = ?",
                   (take["duration_s"], url, take["overlay_applied"], take_id))
        def put(table, r):
            # With its old id where that is still free (another take's row may
            # have taken it since), else as a new row.
            r = {**r, "take_id": take_id}
            try:
                db.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                           list(r.values()))
            except sqlite3.IntegrityError:
                r.pop("id", None)
                db.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                           list(r.values()))

        db.execute("DELETE FROM overlays WHERE take_id = ?", (take_id,))
        for o in snap["overlays"]:
            put("overlays", o)
        # Sidecars written before narrations had rows have none, and leave them alone.
        if "narrations" in snap:
            db.execute("DELETE FROM narrations WHERE take_id = ?", (take_id,))
            for n in snap["narrations"]:
                put("narrations", n)

    def undo_take(self):
        # POST /takes/undo {id}: take back the take's current edit. The take
        # as it is now (its files, to media/.trash/ as <stem>-after-…; its
        # row, items and narrations, into the sidecar's "undone" mark) is kept
        # for Redo, and the files and rows from before the edit come back.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db, row = self.take_row(body.get("id"))
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        with EDIT_LOCK:
            edits, cursor, _, why = history_state(row["file"])
            if cursor is None:
                return self.send_json(409, {"error": why or "nothing to undo"})
            name, saved = edits[cursor]
            src = os.path.join(ROOT, row["file"])
            clean = os.path.join(ROOT, ".clean", row["file"])
            after = {"take": dict(row), "files": {},
                     "overlays": [dict(r) for r in db.execute(
                         "SELECT * FROM overlays WHERE take_id = ? ORDER BY id", (row["id"],))],
                     "narrations": [dict(r) for r in db.execute(
                         "SELECT * FROM narrations WHERE take_id = ? ORDER BY id", (row["id"],))]}
            # Everything the take has now steps aside (a clean copy made since
            # the edit too), then the files from before the edit come back.
            for path in [src] + ([clean] if os.path.isfile(clean) else []):
                rel = os.path.relpath(path, ROOT)
                kept = name[:-5].replace("-before-", "-clean-after-" if path == clean else "-after-") + ".mp4"
                shutil.move(path, os.path.join(TRASH, kept))
                after["files"][rel] = kept
            for orig, trashed in saved["files"].items():
                shutil.move(os.path.join(TRASH, trashed), os.path.join(ROOT, orig))
            self.restore_rows(db, row["id"], saved)
            db.commit()
            after["stat"] = file_stat(src)
            saved["undone"] = after
            with open(os.path.join(TRASH, name), "w") as f:
                json.dump(saved, f, indent=1)
            self.mark_take_cut(row["file"], None)
        self.send_json(200, {"id": row["id"], "undone": edit_what(name, saved)})

    def redo_take(self):
        # POST /takes/redo {id}: put back the edit just above the cursor: the
        # files from before it return to media/.trash/ and the take as it was
        # after the edit, kept by undo_take, comes back.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db, row = self.take_row(body.get("id"))
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        with EDIT_LOCK:
            edits, _, redo, _ = history_state(row["file"])
            if redo is None:
                return self.send_json(409, {"error": "nothing to redo"})
            name, saved = edits[redo]
            after = saved["undone"]
            src = os.path.join(ROOT, row["file"])
            for orig, trashed in saved["files"].items():
                shutil.move(os.path.join(ROOT, orig), os.path.join(TRASH, trashed))
            for orig, kept in after["files"].items():
                os.makedirs(os.path.dirname(os.path.join(ROOT, orig)), exist_ok=True)
                shutil.move(os.path.join(TRASH, kept), os.path.join(ROOT, orig))
            self.restore_rows(db, row["id"], after)
            db.commit()
            del saved["undone"]
            saved["after"] = file_stat(src)
            with open(os.path.join(TRASH, name), "w") as f:
                json.dump(saved, f, indent=1)
            kind = edit_kind(name, saved)
            self.mark_take_cut(row["file"], {kind: saved.get(kind)})
        self.send_json(200, {"id": row["id"], "redone": edit_what(name, saved)})

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
        if self.recording() or testing.locked() or narration["current"]:
            return self.send_json(409, {"error": "the mic is busy (recording, narrating or testing)"})
        if dictation["current"]:
            return self.send_json(409, {"error": dictating_where()})
        with EVENTS_LOCK:
            pending_events.clear()
        log = open(os.path.join(HERE, "record.log"), "a")
        recorder = subprocess.Popen([os.path.join(HERE, "record.sh"), source_paths()[source]],
                                    cwd=HERE, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.send_json(202, {"started": True})

    def record_event(self):
        # {events: [...]}: events the page logs as they happen (everything but
        # pointer samples), each stamped with its arrival time. Never an
        # error for the page to show: with nothing recording, nothing is kept.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        events = [e for e in body.get("events") or [] if isinstance(e, dict)]
        if not self.recording():
            return self.send_json(200, {"logged": 0})
        now = int(time.time() * 1000)
        log_events([{**e, "serverMs": now} for e in events])
        self.send_json(200, {"logged": len(events)})

    def stop_recording(self):
        # The session's events.json is the page's event array plus whatever the
        # server's own log (record_event) holds that the array lacks; then
        # .record-stop tells record.sh the take is over. The array as posted
        # is kept as events-page.json, and one "events:" log line says what
        # each side had and what the page's array was missing.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not self.recording():
            return self.send_json(409, {"error": "nothing is recording"})
        try:
            session = open(os.path.join(ROOT, ".record-session")).read().strip()
        except OSError:
            return self.send_json(409, {"error": "the recorder hasn't started yet"})
        page = [e for e in body.get("events", []) if isinstance(e, dict)]
        log_events([])  # anything still waiting for the session directory
        logged = []
        try:
            with open(os.path.join(session, "events.jsonl")) as f:
                logged = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError) as e:
            print(f"events: no server log for {os.path.basename(session)} ({e})", flush=True)
        have = {event_key(e) for e in page}
        missing = [e for e in logged if event_key(e) not in have]
        events = page
        json.dump(page, open(os.path.join(session, "events-page.json"), "w"), indent=1)
        if missing:
            # sorted() is stable, so events with one wallMs keep their order.
            events = sorted(page + missing, key=lambda e: e.get("wallMs") or 0)
        kinds = lambda evs: dict(collections.Counter(e.get("event") for e in evs if e.get("event") != "pointer"))
        print(f"events: {os.path.basename(session)} page {kinds(page)} server {kinds(logged)}; "
              f"the page's array was missing {len(missing)}"
              + (f": {[(e.get('event'), e.get('cid')) for e in missing]}" if missing else ""), flush=True)
        json.dump(events, open(os.path.join(session, "events.json"), "w"), indent=1)
        # What the source player reported at Stop, so an empty take carries evidence.
        json.dump(body.get("diag", {}), open(os.path.join(session, "diag.json"), "w"), indent=1)
        open(os.path.join(ROOT, ".record-stop"), "w").close()
        self.send_json(200, {"stopped": True, "events": len(events), "recovered": len(missing)})

    def run_voicetest(self):
        # One 10s test at a time, never during a take; returns when it's measured.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if dictation["current"]:
            return self.send_json(409, {"error": dictating_where()})
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
    _db.execute(NARRATIONS_SQL)
recorder = adopt_running_take()
backfill_studio_files()
threading.Thread(target=watch_changes, daemon=True).start()
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
