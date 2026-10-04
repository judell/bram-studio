#!/usr/bin/env python3
"""Serve the open project's media/ with video content types and HTTP byte ranges.

    python3 serve_media.py [port]      (default 8765)

Bram's loopback serves project files but has no video content types and
no Range support, so a browser would download an MP4 instead of playing
it (and couldn't seek). This fills that gap until Bram does it itself.

The open project (studio_paths.py) is fixed at startup: its media/ is what
is served, and POST /query {sql, params} answers the pages' read-only
queries from its studio.db. /tests/<name> serves the Audio bench's shared
recordings. GET /projects lists the project folders, with what each one's
disk space is for and what in it isn't Studio's (project_usage);
POST /projects/delete {slug} moves a whole project folder (not the open one)
to the system Trash under its own name; POST /projects/stray/delete
{slug, path} moves a stray item to the system Trash, and POST /projects/clean {slug, what, days, dry} does the same
for a project's discarded files, deleted takes (restorable ones), or undo
history; POST /projects/open
{slug} and /projects/new {name} switch to one, by starting this server over
on it (refused while a take, narration, render, export or edit is under way).

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
import studio_paths
import voicetest

HERE = studio_paths.REPO  # the scripts, record.log
# The open project (studio_paths.py): its takes and exports, its database and
# its render ingredients. The scripts launched from here inherit it.
ROOT, DB, WORK = studio_paths.MEDIA, studio_paths.DB, studio_paths.WORK
os.environ["STUDIO_PROJECT"] = studio_paths.PROJECT
recorder = None  # the running record.sh, if any
testing = threading.Lock()  # held while voicetest.py has the mic
SOURCES = studio_paths.SOURCES  # source movies, usually symlinks
DESKTOP = os.path.expanduser("~/Desktop")  # where recordings usually land


PHASES = {"starting": "Starting the recorder…",
          "recording": "Recording: click Stop to finish",
          "rendering": "Rendering the take…",
          "naming": "Naming the take from its narration…"}


# POSTs in flight, and whether the server is about to start over on another
# project (Handler.open_project): from then on new POSTs are turned away.
POSTS, POSTS_LOCK = {"n": 0, "restarting": False}, threading.Lock()
exporting = threading.Lock()  # one export at a time
overlaying = threading.Lock()  # one overlay.py render at a time
SPRITE_WHERE = {}  # recording sprite tag -> where it goes (0-1 picture box)


def probe(path, entries, stream=None):
    args = ["ffprobe", "-v", "error", *(["-select_streams", stream] if stream else []),
            "-show_entries", entries, "-of", "csv=p=0", path]
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


# The take editor's audio strip: a take's sound level in LEVEL_BINS equal
# slices, measured from the file its player plays (the clean copy, as
# /clean/ serves it, or the take). RMS per slice in dB, by ffmpeg's astats
# over 16 kHz mono (the recipe an earlier level check used); a silent slice
# reads -inf, sent as -120. Cached by path and mtime: every edit rewrites
# the file, so an edited take is measured again and an unchanged one isn't.
LEVEL_BINS = 200
LEVELS_CACHE = {}


def take_levels(path):
    key = (path, os.path.getmtime(path))
    if key in LEVELS_CACHE:
        return LEVELS_CACHE[key]
    dur = float(probe(path, "format=duration") or 0)
    if dur <= 0:
        return {"binSecs": 0, "levels": []}
    per = max(1, round(dur * 16000 / LEVEL_BINS))
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-af",
                          f"aresample=16000,asetnsamples=n={per}:p=0,astats=metadata=1:reset=1,"
                          "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                          "-f", "null", "-"], capture_output=True, text=True).stdout
    levels = []
    for m in re.finditer(r"RMS_level=(\S+)", out):
        try:
            v = float(m.group(1))
        except ValueError:
            v = -120.0
        levels.append(round(max(v, -120.0), 1) if v == v else -120.0)
    result = {"binSecs": round(per / 16000, 4), "levels": levels}
    LEVELS_CACHE.clear()  # one take is edited at a time; keep only the latest
    LEVELS_CACHE[key] = result
    return result


# One item's entry as Apply records it (overlay.APPLIED_SQL joins these
# with |): made with the same SQL so numbers format identically, for telling
# whether an item is in its scene's video as it is now.
APPLIED_ENTRY = ("id || char(58) || text || char(58) || x1 || char(58) || y1 || char(58) || x2 || char(58) || y2 "
                 "|| char(58) || t_in || char(58) || t_out || char(58) || ifnull(tail, char(45)) || char(58) "
                 "|| ifnull(shape, char(45))")


def apply_scene(take_id):
    # Apply: burn a scene's items into its file (overlay.py render), from its
    # clean copy. Apply rewrites the file without being an edit, so the
    # scene's edit history is kept pointing at it (resync_history). The
    # caller holds `overlaying`. Returns (True, overlay.py's result) or
    # (False, the error).
    row = sqlite3.connect(DB).execute("SELECT file FROM takes WHERE id = ?", (take_id,)).fetchone()
    try:
        was = file_stat(os.path.join(ROOT, row[0])) if row else None
    except OSError:
        was = None
    r = subprocess.run(["python3", os.path.join(HERE, "overlay.py"), "render", str(take_id)],
                       cwd=HERE, capture_output=True, text=True)
    if r.returncode:
        return False, (r.stderr.strip().splitlines() or ["render failed"])[-1]
    if was:
        resync_history(row[0], was)
    return True, json.loads(r.stdout.strip().splitlines()[-1])


def scene_callout_box(file, text):
    # A scene callout's box (scene_callouts): the name laid out as overlay.py
    # draws it in the widest box allowed, then the box's left edge moved in
    # to the text's width so it hugs the top-right corner. The text keeps its
    # size, since it still fits exactly.
    try:
        W, H = overlay.video_size(os.path.join(ROOT, file))
    except Exception:
        return 0.31, 0.03, 0.97, 0.14
    L = overlay.layout({"kind": "callout", "text": text, "x1": 0.31, "y1": 0.03, "x2": 0.97, "y2": 0.14}, W, H)
    return round(max(0.31, 0.97 - L["bw"] / W - 0.002), 4), 0.03, 0.97, 0.14


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


# What a project's disk space is for (the Projects page): key, label, what it
# is. project_usage() puts every file in exactly one.
USAGE = (("takes", "Takes", "the takes, their clean copies, replaced narration audio, previews"),
         ("remix", "Re-mix ingredients", "sessions and tracks of the takes in the list"),
         ("exports", "Exports", "everything on the Exports page"),
         ("logs", "Logs", "logs Studio wrote, kept for tracing what happened"),
         ("undo", "Undo history", "what the History tab can still restore"),
         ("deleted", "Deleted takes", "takes you deleted that Studio can still restore, with their recordings"),
         ("discarded", "Discarded", "what nothing in Studio uses or can restore: deleted exports, takes deleted "
          "before Undo delete existed, abandoned recordings, render leftovers"),
         ("stray", "Stray", "not Studio's: listed below"))
MOVIES = (".mp4", ".mov", ".m4v")
# In media/.trash/: a step of a take's edit history (apply_take_edit, remix_take.py).
HISTORY_FILE = re.compile(r"(.+?)(-clean)?-(before|after)-(cut|uncut|pause|unpause|narrate|unnarrate|remix)-\d{8}-\d{6}\.\w+$")


# The clean-up buttons on the Projects page, per category: (label, days, what
# the confirmation says is lost). days: only what was deleted at least that
# long ago. Takes, re-mix ingredients and exports have none.
CLEAN = {"discarded": [("Clean up", 0, "Nothing in Studio uses these or can restore them; the takes in the list "
                        "aren't affected.")],
         # Every category's button is "Clean up"; deleted takes also offer the
         # older ones alone, while there are any.
         "deleted": [("Clean up", 0, "These deleted takes can then no longer be restored in Studio."),
                     ("Only older than 7 days", 7, "Takes deleted more than 7 days ago can then no longer "
                      "be restored in Studio.")],
         "undo": [("Clean up", 0, "The takes stay as they are now. Their History starts over, so the edits made "
                   "so far can no longer be undone.")]}


# What each clean-up costs, said beside its button and first in its
# confirmation, so it is plain which ones end an undo and which end nothing.
# (Re-mixing survives all three: remix_take.py --regate replays a take's cuts
# and pauses from its own JSON, not from the undo history.)
LOSES = {"undo": "Undo for every edit made so far",
         "deleted": "Undo delete for these takes",
         "discarded": "nothing Studio uses"}


# What depends on the operating system is in these three functions.
def disk_size(st):
    # A file's size on disk, from its stat. Python has st_blocks on macOS and
    # Linux, not on Windows: there, the file's length.
    return st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size


def trashed_at(path):
    # When something was moved into a project's .trash. A move changes the
    # file's change time (st_ctime) on macOS and Linux and leaves its
    # modification time alone. (On Windows st_ctime is the creation time;
    # nothing is cleaned by age there until trash_path has a Windows answer.)
    return os.lstat(path).st_ctime


def trash_path(name):
    # Where something called `name` goes in the system Trash, with the time
    # in its name so it can't land on something already there. A move within
    # the disk is instant whatever the size, and a wrong click is recoverable
    # until the Trash is emptied. macOS only so far: the Windows Recycle Bin
    # and Linux's trash folder each need their own answer here.
    if sys.platform != "darwin":
        raise OSError("Studio can only move things to the Trash on macOS for now")
    stem, ext = os.path.splitext(name)
    return os.path.expanduser(f"~/.Trash/{stem} {time.strftime('%Y-%m-%d %H.%M.%S')}{ext}")


def tree_size(path):
    # (bytes on disk, files) under a file or a folder; links aren't followed.
    if os.path.islink(path) or not os.path.isdir(path):
        return disk_size(os.lstat(path)), 1
    size = files = 0
    for root, _, names in os.walk(path):
        for n in names:
            try:
                size += disk_size(os.lstat(os.path.join(root, n)))
                files += 1
            except OSError:
                pass
    return size, files


def take_keys(folder, files):
    # For take files (take-<stamp>-raw.mp4): their stems in media/, their
    # names in work/takes/ and work/narrated/, and their session folders.
    media, work, sessions = set(), set(), set()
    for f in files:
        stem = os.path.splitext(f)[0]
        name = stem[:-len("-raw")] if stem.endswith("-raw") else stem
        media.add(stem)
        work.add(name)
        try:
            with open(os.path.join(folder, "work", "takes", name + ".json")) as fh:
                sessions.add(os.path.basename(json.load(fh)["session"]))
        except (OSError, ValueError, KeyError, TypeError):
            sessions.add(name[len("take-"):] if name.startswith("take-") else name)
    return media, work, sessions


def of_take(name, takes):
    # Is this work/ file one of those takes'? take-<stamp>.json, -source.wav...
    return any(name.startswith(t) and name[len(t):len(t) + 1] in (".", "-", "") for t in takes)


def project_usage(folder):
    # {bytes, categories: [{key, label, about, bytes, files}], stray: [{path,
    # bytes, folder}]} for a project folder. Everything Studio puts there is
    # accounted for by name; what is left is stray: an editor's project
    # folder, a file dropped in. In exports/ an .mp4 is expected whoever
    # wrote it (an edited export is saved there); anything else is stray.
    # For the clean-up buttons (clean_project) it also returns _paths, each
    # category's files and folders, and _when, when each deleted one was
    # deleted.
    # Deleted means restorable: a take Studio's Undo delete can bring back,
    # from its record in .trash. Anything else in .trash (a deleted export,
    # which has no undo; a take deleted before Undo delete existed) is
    # discarded, with what else nothing uses.
    live_files, deleted_files, deleted_at, restorable = set(), set(), {}, set()
    try:
        db = sqlite3.connect(f"file:{urllib.parse.quote(os.path.join(folder, 'studio.db'))}?mode=ro", uri=True)
        try:
            live_files = {f for (f,) in db.execute("SELECT file FROM takes")}
        finally:
            db.close()
    except sqlite3.Error:
        pass
    media, work = os.path.join(folder, "media"), os.path.join(folder, "work")
    trash = os.path.join(media, ".trash")

    def entries(path):
        try:
            return sorted(n for n in os.listdir(path) if n != ".DS_Store")
        except OSError:
            return []

    # Deleted takes that can still be restored: their sidecars in .trash.
    for n in entries(trash):
        if n.endswith(".json") and not n.startswith(".") and not HISTORY_FILE.match(n):
            try:
                with open(os.path.join(trash, n)) as fh:
                    record = json.load(fh)
                file = record["take"]["file"]
                deleted_files.add(file)
                restorable |= {n, *record.get("files", {}).values()}  # the record and what it restores
                # Its recordings in work/ were deleted when it was.
                _, names, sessions = take_keys(folder, [file])
                for key in names | sessions:
                    deleted_at[key] = trashed_at(os.path.join(trash, n))
            except (OSError, ValueError, KeyError, TypeError):
                pass
    live_media, live_work, live_sessions = take_keys(folder, live_files)
    _, deleted_work, deleted_sessions = take_keys(folder, deleted_files - live_files)
    size, count, stray = collections.Counter(), collections.Counter(), []
    paths, when = collections.defaultdict(list), {}

    def add(kind, path):
        b, n = tree_size(path)
        size[kind] += b
        count[kind] += n
        paths[kind].append(path)
        if kind == "deleted":
            name = os.path.basename(path)
            if os.path.dirname(path) == trash:
                when[path] = trashed_at(path)
            else:
                when[path] = next((t for key, t in deleted_at.items() if name == key or of_take(name, [key])), 0)
        if kind == "stray":
            stray.append({"path": os.path.relpath(path, folder), "bytes": b,
                          "folder": os.path.isdir(path) and not os.path.islink(path)})

    for n in entries(folder):
        if n in ("media", "work"):
            continue
        add("takes" if n == "project.json" or n.startswith("studio.db") else "stray", os.path.join(folder, n))
    for n in entries(media):
        p = os.path.join(media, n)
        if n in live_files or n in (".narrated", ".preview") or n.startswith(".record-"):
            add("takes", p)
        elif n == ".clean":
            for c in entries(p):
                add("takes" if c in live_files else "stray", os.path.join(p, c))
        elif n == ".trash":
            for c in entries(p):
                m = HISTORY_FILE.match(c)
                add("undo" if m and m.group(1) in live_media else "deleted" if c in restorable else "discarded",
                    os.path.join(p, c))
        elif n == "exports":
            for c in entries(p):
                cp = os.path.join(p, c)
                ours = os.path.isfile(cp) and (c.startswith(".") or c.lower().endswith(".mp4"))
                add("exports" if ours else "stray", cp)
        else:
            add("stray", p)
    for n in entries(work):
        p = os.path.join(work, n)
        if n == "sessions":
            for c in entries(p):
                add("remix" if c in live_sessions else "deleted" if c in deleted_sessions else "discarded",
                    os.path.join(p, c))
        elif n == "takes":
            for c in entries(p):
                add("remix" if of_take(c, live_work) else "deleted" if of_take(c, deleted_work) else "discarded",
                    os.path.join(p, c))
        elif n == "narrated" or n.startswith("overlay-"):
            # Renders on their way to media/ and overlay.py's scratch folders:
            # nothing reads them once the take is registered.
            add("discarded", p)
        elif n.endswith(".log") and os.path.isfile(p):
            # A log (serve_media.log ran here before it moved to the repo):
            # kept for forensics, so neither stray nor offered for clean-up.
            add("logs", p)
        else:
            add("stray", p)
    return {"bytes": sum(size.values()),
            "categories": [{"key": k, "label": label, "about": about, "bytes": size[k], "files": count[k]}
                           for k, label, about in USAGE],
            "stray": stray, "_paths": paths, "_when": when}


def clean_paths(folder, what, days, usage=None):
    # What a clean-up button would remove: that category's files and folders,
    # for "deleted" only those deleted at least `days` days ago. usage: the
    # folder's project_usage, when it is already at hand.
    usage = usage or project_usage(folder)
    found = usage["_paths"].get(what, []) if what in CLEAN else []
    if what == "deleted" and days:
        found = [p for p in found if usage["_when"].get(p, 0) <= time.time() - days * 86400]
    return found


TAKE_NAME = re.compile(r"(take-\d{8}-\d{6}|perf-\d+)")  # the take a work/ or .trash file is part of
TREE_ITEMS = 100  # entries listed per folder in the confirmation; the rest are counted


def clean_summary(folder, what, found):
    # (summary, tree) for a clean-up's confirmation. The summary counts what
    # means something (sessions, takes, edits), not files: a take recorded
    # with ink leaves a picture per frame, so files run to the thousands. The
    # tree is two levels: the folders things go from, largest first, and in
    # each what goes (a folder moved whole, like work/narrated, lists what is
    # in it), largest first.
    rel = [os.path.relpath(p, folder) for p in found]

    def takes_in(names):
        return {m.group(1) for m in (TAKE_NAME.match(os.path.basename(n)) for n in names) if m}

    sessions = [r for r in rel if os.path.dirname(r) == os.path.join("work", "sessions")]
    if what == "discarded":
        tracks = takes_in(r for r in rel if os.path.dirname(r) == os.path.join("work", "takes"))
        renders = set()
        for p in found:
            if os.path.basename(p) == "narrated" and os.path.isdir(p):
                for root, dirs, names in os.walk(p):
                    renders |= takes_in(dirs + names)
        parts = []  # only what there is: no "0 recording sessions"
        trashed = [r for r in rel if os.path.dirname(r) == os.path.join("media", ".trash")]
        if trashed:
            parts.append(f"{len(trashed)} file{'s' * (len(trashed) != 1)} from the project's trash that Studio "
                         "can't restore (deleted exports, takes deleted before Undo delete)")
        if sessions:
            parts.append(f"{len(sessions)} recording session{'s' * (len(sessions) != 1)} of takes no longer in the list")
        if tracks:
            parts.append(f"the separate tracks of {len(tracks)} take{'s' * (len(tracks) != 1)} no longer in the list")
        if renders:
            parts.append(f"the render leftovers of {len(renders)} take{'s' * (len(renders) != 1)}")
        parts = parts or ["leftover files"]
        summary = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    elif what == "deleted":
        records = [r for r in rel if os.path.dirname(r) == os.path.join("media", ".trash") and r.endswith(".json")]
        summary = (f"{len(records)} deleted take{'s' * (len(records) != 1)}"
                   + (f", with the recordings of {len(sessions)} of them" if sessions else ""))
    else:
        edits = [r for r in rel if r.endswith(".json")]
        takes = {HISTORY_FILE.match(os.path.basename(r)).group(1) for r in edits if HISTORY_FILE.match(os.path.basename(r))}
        summary = (f"the undo history of {len(takes)} take{'s' * (len(takes) != 1)}: "
                   f"{len(edits)} edit{'s' * (len(edits) != 1)} and the copies of the take from before each")
    groups = collections.defaultdict(list)
    for p, r in zip(found, rel):
        if os.path.isdir(p) and not os.path.islink(p) and os.path.dirname(r) == "work":
            for c in sorted(os.listdir(p)):
                groups[r].append(os.path.join(p, c))
        else:
            groups[os.path.dirname(r)].append(p)
    tree = []
    for g, ps in groups.items():
        items = sorted(({"name": os.path.basename(p), "folder": os.path.isdir(p) and not os.path.islink(p),
                         **dict(zip(("bytes", "files"), tree_size(p)))} for p in ps), key=lambda i: -i["bytes"])
        tree.append({"path": g + "/",
                     "bytes": sum(i["bytes"] for i in items), "files": sum(i["files"] for i in items),
                     "count": len(items), "items": items[:TREE_ITEMS], "more": max(0, len(items) - TREE_ITEMS)})
    return summary, sorted(tree, key=lambda t: -t["bytes"])


def shared_stray():
    # Stray items in what projects share: anything in sources/ that isn't a
    # movie or a link to one, and, once Studio's own takes have moved into a
    # project, anything in the repo's media/ but the Audio bench's tests/.
    found = []

    def add(path):
        found.append({"path": os.path.relpath(path, HERE), "bytes": tree_size(path)[0],
                      "folder": os.path.isdir(path) and not os.path.islink(path)})

    try:
        for n in sorted(os.listdir(SOURCES)):
            p = os.path.join(SOURCES, n)
            if n != ".DS_Store" and not (n.lower().endswith(MOVIES) and not os.path.isdir(p)):
                add(p)
    except OSError:
        pass
    if studio_paths.PROJECT != HERE:
        try:
            for n in sorted(os.listdir(os.path.join(HERE, "media"))):
                if n not in ("tests", ".DS_Store"):
                    add(os.path.join(HERE, "media", n))
        except OSError:
            pass
    return found


# Safekeeping (the Projects page): a copy of a project, in a folder the user
# names (a OneDrive folder, say; nothing here knows which service), of what
# can't be made again and what re-mixing needs. The categories it copies; the
# database and project.json are added by copy_project. Undo history, deleted
# takes, discarded files, stray files and the shared source movies stay out.
KEEP = ("takes", "remix", "exports", "logs")
SAFE_LOCK = threading.Lock()  # one copy at a time


def read_project(folder):
    try:
        with open(os.path.join(folder, "project.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_project(folder, info):
    tmp = os.path.join(folder, ".project.json.tmp")
    with open(tmp, "w") as f:
        json.dump(info, f, indent=1)
    os.replace(tmp, os.path.join(folder, "project.json"))


def keep_files(folder, usage=None):
    # [(path, path within the project)] of every file a safekeeping copy takes,
    # but the database (taken with SQLite's backup) and project.json. The
    # previews' cache and a recording's state files are left out.
    usage = usage or project_usage(folder)
    out = []
    for kind in KEEP:
        for p in usage["_paths"].get(kind, []):
            n = os.path.basename(p)
            if n == ".preview" or n.startswith(".record-") or n.startswith("studio.db") or n == "project.json":
                continue
            if os.path.isdir(p) and not os.path.islink(p):
                for root, _, names in os.walk(p):
                    out += [(os.path.join(root, f), os.path.relpath(os.path.join(root, f), folder))
                            for f in names if f != ".DS_Store"]
            else:
                out.append((p, os.path.relpath(p, folder)))
    return out


def same_file(a, b):
    # Already safe: the copy has the same size and modification time
    # (shutil.copy2 carries the time over). Exact: a two-second allowance for
    # folders that round times skipped a file changed within two seconds; a
    # folder that rounds now gets its files copied again, never missed.
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return sa.st_size == sb.st_size and abs(sa.st_mtime - sb.st_mtime) < 0.001


def safekeeping_state(folder, info, usage):
    # What the Projects page shows: the location, the last copy, and whether
    # the project has changed since (a kept file newer than the copy, or
    # one not copied yet).
    last, loc = info.get("last_copy"), info.get("safekeeping") or ""
    changed = None
    if last:
        at = last.get("at_s", 0)
        changed = any(os.path.getmtime(p) > at for p, _ in keep_files(folder, usage)) or \
            os.path.getmtime(os.path.join(folder, "studio.db")) > at
    copy_path = os.path.join(loc, os.path.basename(folder)) if loc else ""
    return {"location": loc, "exists": bool(loc) and os.path.isdir(loc), "last": last, "changed_since": changed,
            "copy_path": copy_path, "copy_exists": bool(copy_path) and os.path.isdir(copy_path)}


def project_info(slug):
    # One row of the Projects page, or None for a folder that isn't a project
    # (no project.json). Its takes are counted from its own database.
    folder = os.path.join(studio_paths.PROJECTS, slug)
    info = read_project(folder)
    if info is None:
        return None
    name = info.get("name") or slug
    takes, seconds, changed, db_path = 0, 0.0, None, os.path.join(folder, "studio.db")
    try:
        changed = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(db_path)))
        db = sqlite3.connect(f"file:{urllib.parse.quote(db_path)}?mode=ro", uri=True)
        try:
            takes, seconds = db.execute("SELECT count(*), coalesce(sum(duration_s), 0) FROM takes").fetchone()
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        pass
    full = project_usage(folder)
    usage = {k: v for k, v in full.items() if not k.startswith("_")}
    for s in usage["stray"]:
        s["slug"] = slug  # each row of the page's list says whose it is
    for c in usage["categories"]:
        # Its clean-up buttons, each saying what it is for (clean_project):
        # only those that would remove something now, so "older than 7 days"
        # goes away once nothing deleted is that old.
        c["loses"] = LOSES.get(c["key"], "")
        c["actions"] = [{"slug": slug, "project": name, "what": c["key"], "category": c["label"], "label": label,
                         "days": days, "warning": warning, "loses": c["loses"]}
                        for label, days, warning in CLEAN.get(c["key"], [])
                        if clean_paths(folder, c["key"], days, full)]
    return {"slug": slug, "name": name, "takes": takes, "seconds": round(seconds, 1), "changed": changed,
            "open": folder == studio_paths.PROJECT, "safekeeping": safekeeping_state(folder, info, full), **usage}


ORDER = os.path.join(studio_paths.PROJECTS, ".order")  # the Projects page's drag order: a JSON list of slugs


def write_order(slugs):
    tmp = ORDER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(slugs, f)
    os.replace(tmp, ORDER)


def read_order():
    try:
        with open(ORDER) as f:
            order = json.load(f)
        return [s for s in order if isinstance(s, str)] if isinstance(order, list) else []
    except (OSError, ValueError):
        return []


def list_projects():
    # {open, name, projects, shared_stray, stray_count}: every project folder,
    # by name, with what its disk space is for (project_usage), and which one
    # this server has open (none, in a Studio from before projects). Sizes
    # are worked out per request: about 0.2 s for 16,000 files.
    try:
        slugs = [s for s in os.listdir(studio_paths.PROJECTS) if not s.startswith(".")]
    except OSError:
        slugs = []
    projects = sorted(filter(None, map(project_info, slugs)), key=lambda p: p["name"].lower())
    order = read_order()
    if order:
        # The Projects page's drag order (projects/.order). Projects it
        # doesn't name yet (made since, or copied in) come first, newest
        # first, as new takes go to the top of the Takes list.
        def made(p):
            st = os.stat(os.path.join(studio_paths.PROJECTS, p["slug"]))
            return getattr(st, "st_birthtime", st.st_mtime)
        new = sorted((p for p in projects if p["slug"] not in order), key=made, reverse=True)
        projects = new + sorted((p for p in projects if p["slug"] in order), key=lambda p: order.index(p["slug"]))
    for p in projects:
        p["id"] = p["slug"]  # what the page's drag list keys rows by
    current = next((p for p in projects if p["open"]), None)
    shared = [{**s, "slug": ""} for s in shared_stray()]
    return {"open": current and current["slug"], "name": current["name"] if current else "", "projects": projects,
            "shared_stray": shared, "stray_count": len(shared) + sum(len(p["stray"]) for p in projects),
            "folder": studio_paths.PROJECTS}


def restart():
    # Opening another project starts the server over on it. Every path here
    # was worked out once, at startup (ROOT, DB, WORK and what is built from
    # them), so starting over can't leave one pointing at the old project,
    # where looking each up at the moment of use could miss a site. Nothing
    # else is running: open_project checked, and new POSTs are turned away.
    time.sleep(0.3)  # the response to the click goes out first
    os.environ.pop("STUDIO_PROJECT", None)  # projects/.open decides now
    print(f"restarting to open {open(studio_paths.OPEN).read().strip()}", flush=True)
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])


def fingerprints():
    # What each of the page's lists is built from, cheaply: the recording
    # phase, studio.db (takes, callouts; register.py writes it too), the two
    # source folders, the exports folder and the project folders. A change
    # means "refetch that".
    def mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None
    return {"record": json.dumps(record_status(recorder is not None and recorder.poll() is None)),
            "takes": mtime(DB),
            "sources": (mtime(SOURCES), mtime(DESKTOP)),
            "exports": exports_fingerprint(),
            "projects": projects_fingerprint(mtime)}


def projects_fingerprint(mtime):
    # For the Projects page, which measures every project folder (too slow to
    # do every second): the modification times of projects/, the drag order,
    # and each project's folder and top-level folders. A file or folder
    # dropped in from outside, a project added or removed, or a new order
    # changes one of them; changes inside the open project's database and
    # exports arrive as "takes" and "exports" too.
    found = [mtime(studio_paths.PROJECTS), mtime(ORDER)]
    try:
        slugs = sorted(s for s in os.listdir(studio_paths.PROJECTS) if not s.startswith("."))
    except OSError:
        slugs = []
    for slug in slugs:
        base = os.path.join(studio_paths.PROJECTS, slug)
        found += [mtime(os.path.join(base, *p)) for p in
                  ((), ("project.json",), ("studio.db",), ("media",), ("media", "exports"), ("media", ".trash"),
                   ("media", ".clean"), ("work",), ("work", "sessions"), ("work", "takes"))]
    return found


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
EDIT_SIDECAR = re.compile(r"-before-(cut|uncut|pause|unpause|narrate|unnarrate)-\d{8}-\d{6}\.json$")
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
# A take's inserted pauses (the Pause tab), so each can be listed, marked on
# its slider and removed at any time, as narrations can: where the held frame
# starts and how long it lasts. A later cut before one moves it; one through
# its held stretch drops the row. A pause inserted before it moves it; one
# inside its held stretch lengthens it. Removing one cuts its held stretch
# out (/pauses/remove). The page lists them through /query.
# A scene's cuts (the Cut tab), so each can be listed, its join replayed and
# the removed stretch restored at any time: where the seam is now (at), how
# long the stretch was, and where to find it: the cut's undo record
# (sidecar), which names the scene's files from before the cut, and where the
# stretch starts in them (src_start). A later cut before the seam moves it;
# one containing it drops the row. A pause or a restored stretch before it
# moves it later. Restoring splices the stretch back (/cuts/restore).
CUTS_SQL = """CREATE TABLE IF NOT EXISTS cuts (
  id INTEGER PRIMARY KEY,
  take_id INTEGER NOT NULL,
  at REAL NOT NULL,
  seconds REAL NOT NULL,
  sidecar TEXT NOT NULL,
  src_start REAL NOT NULL,
  created_at TEXT NOT NULL
)"""
PAUSES_SQL = """CREATE TABLE IF NOT EXISTS pauses (
  id INTEGER PRIMARY KEY,
  take_id INTEGER NOT NULL,
  at REAL NOT NULL,
  seconds REAL NOT NULL,
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
    if kind == "uncut":
        return f"Restored the {rec[1]:g} s cut at {rec[0]:.2f} s"
    if kind == "unpause":
        return f"Removed the {rec[1] - rec[0]:g} s pause at {rec[0]:.2f} s"
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
        if name.startswith("/tests/"):
            # The Audio bench's recordings (voicetest.py) are shared, not the
            # open project's. Only plain file names.
            name = name[len("/tests/"):]
            if "/" in name or name.startswith("."):
                return os.path.join(ROOT, ".no-such-file")
            return os.path.join(studio_paths.TESTS, name)
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
        if self.path == "/projects":
            return self.send_json(200, list_projects())
        if self.path == "/dictation/status":
            return self.dictation_status()
        if self.path == "/dictation/engine":
            return self.dictation_engine()
        if self.path.startswith("/takes/edits"):
            return self.list_edits()
        if self.path.startswith("/takes/levels"):
            return self.take_levels_route()
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
        # Counted, so opening another project (which restarts the server) can
        # tell that no other request is in the middle of changing something.
        # The pages' queries change nothing, so they are neither counted nor
        # turned away.
        if self.path == "/query":
            return self.query()
        with POSTS_LOCK:
            if POSTS["restarting"]:
                return self.send_json(503, {"error": "Studio is opening another project; try again in a moment"})
            POSTS["n"] += 1
        try:
            self.route_post()
        finally:
            with POSTS_LOCK:
                POSTS["n"] -= 1

    def route_post(self):
        if self.path == "/projects/open":
            return self.open_project()
        if self.path == "/projects/new":
            return self.new_project()
        if self.path == "/projects/stray/delete":
            return self.delete_stray()
        if self.path == "/projects/clean":
            return self.clean_project()
        if self.path == "/projects/safekeeping":
            return self.set_safekeeping()
        if self.path == "/projects/copy":
            return self.copy_project()
        if self.path == "/projects/safekeeping/clear":
            return self.clear_safekeeping()
        if self.path == "/projects/reorder":
            return self.reorder_projects()
        if self.path == "/projects/delete":
            return self.delete_project()
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
        if self.path == "/audition/log":
            return self.log_audition()
        if self.path == "/voicetest":
            return self.run_voicetest()
        if self.path == "/delete":
            return self.delete_take()
        if self.path == "/undelete":
            return self.undelete_take()
        if self.path == "/takes/cut":
            return self.cut_take()
        if self.path == "/takes/clip":
            return self.clip_take()
        if self.path == "/takes/rename":
            return self.rename_take()
        if self.path == "/scenes/callouts":
            return self.scene_callouts()
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
        if self.path == "/pauses/remove":
            return self.pause_remove()
        if self.path == "/cuts/restore":
            return self.cut_restore()
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
            db = sqlite3.connect(DB)
            # What each scene's list shows is what goes out: a scene whose
            # items differ from its last Apply (the editor's Apply test) is
            # applied first, one at a time; a failure exports nothing.
            for tid, name, record in db.execute(
                    "SELECT id, name, overlay_applied FROM takes ORDER BY position, created_at DESC").fetchall():
                if (db.execute(overlay.APPLIED_SQL, (tid,)).fetchone()[0] or None) == (record or None):
                    continue
                with overlaying:
                    ok, err = apply_scene(tid)
                if not ok:
                    return self.send_json(500, {"error": f"applying {name} failed, so nothing was exported: {err}"})
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
        db = sqlite3.connect(DB)
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
        db = sqlite3.connect(DB)
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
        db = sqlite3.connect(DB)
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
        db = sqlite3.connect(DB)
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
            ok, out = apply_scene(body["id"])
        finally:
            overlaying.release()
        if not ok:
            return self.send_json(500, {"error": out})
        self.send_json(200, out)

    def reorder_takes(self):
        # The takes list's drag order: each id's index becomes its position.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        ids = [i for i in body.get("ids", []) if isinstance(i, int)]
        db = sqlite3.connect(DB)
        db.executemany("UPDATE takes SET position = ? WHERE id = ?", [(pos, tid) for pos, tid in enumerate(ids)])
        db.commit()
        self.send_json(200, {"reordered": len(ids)})

    def delete_take(self):
        # Move the take's MP4 to media/.trash/ first; drop the row only if that worked.
        # Beside it, <name>.json keeps the take's row and its overlays for /undelete.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(DB)
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
                     "SELECT * FROM narrations WHERE take_id = ? ORDER BY id", (row["id"],))],
                 "pauses": [dict(r) for r in db.execute(
                     "SELECT * FROM pauses WHERE take_id = ? ORDER BY id", (row["id"],))],
                 "cuts": [dict(r) for r in db.execute(
                     "SELECT * FROM cuts WHERE take_id = ? ORDER BY id", (row["id"],))]}
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
        db.execute("DELETE FROM pauses WHERE take_id = ?", (row["id"],))
        db.execute("DELETE FROM cuts WHERE take_id = ?", (row["id"],))
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
        db = sqlite3.connect(DB)
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
        for z in saved.get("pauses", []):
            z = {k: v for k, v in z.items() if k != "id"}
            z["take_id"] = take_id
            db.execute(f"INSERT INTO pauses ({', '.join(z)}) VALUES ({', '.join('?' * len(z))})", list(z.values()))
        for c in saved.get("cuts", []):
            c = {k: v for k, v in c.items() if k != "id"}
            c["take_id"] = take_id
            db.execute(f"INSERT INTO cuts ({', '.join(c)}) VALUES ({', '.join('?' * len(c))})", list(c.values()))
        db.commit()
        os.remove(path)
        self.send_json(200, {"restored": take_id, "name": take["name"], "overlays": len(saved["overlays"])})

    def clip_take(self):
        # {source, start, end}: a new take that is that stretch of a source
        # movie, the capture view's Make take. Encoded as render.py encodes
        # takes (its ENC and FPS; AAC 160k, 48 kHz stereo), so Export can still
        # join takes by stream copy. The source's own audio comes along; a
        # source without audio gets a silent track, so every take has one.
        # register.py adds it at the top of the takes list, named for its
        # source and stretch (rename it in the list).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        path = source_paths().get(str(body.get("source") or ""))
        if not path:
            return self.send_json(404, {"error": f"no source movie named {body.get('source')!r}"})
        try:
            start, end = float(body.get("start")), float(body.get("end"))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "start and end must be numbers"})
        dur = float(probe(path, "format=duration") or 0)
        start, end = max(0.0, start), min(dur, end)
        if end - start < 0.5:
            return self.send_json(400, {"error": "a take must be at least 0.5 s"})
        channels = probe(path, "stream=channels", "a:0")
        has_audio = bool(channels)
        # A mono source goes to both sides at its own level (ffmpeg's own
        # upmix pans it 3 dB down).
        mono = ["-af", "pan=stereo|c0=c0|c1=c0"] if channels.strip() == "1" else []
        # Unique even for clips made in the same second (a second one would
        # otherwise overwrite the first and both rows share its file).
        stamp, n = time.strftime('%Y%m%d-%H%M%S'), 1
        fname = f"take-{stamp}-clip.mp4"
        while os.path.exists(os.path.join(ROOT, fname)):
            n += 1
            fname = f"take-{stamp}-clip{n}.mp4"
        dest = os.path.join(ROOT, fname)
        tmp = os.path.join(ROOT, f".{fname}")
        audio_in = [] if has_audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                            "-i", path, *audio_in, "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-tune", "stillimage",
                            "-pix_fmt", "yuv420p", "-r", "25",
                            *mono, "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest",
                            "-f", "mp4", tmp], capture_output=True, text=True)
        if r.returncode:
            if os.path.exists(tmp):
                os.remove(tmp)
            return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["the clip failed"])[-1]})
        os.replace(tmp, dest)
        # Scene N: one more than the number of scenes or the highest Scene N
        # already named, whichever is larger, so a deleted scene's number
        # isn't handed out again while a later one still has it.
        db = sqlite3.connect(DB)
        names = [r[0] for r in db.execute("SELECT name FROM takes")]
        nums = [int(m.group(1)) for m in (re.fullmatch(r"Scene (\d+)", n or "") for n in names) if m]
        name = f"Scene {max([len(names)] + nums) + 1}"
        reg = subprocess.run([sys.executable, os.path.join(HERE, "register.py"), dest, name,
                              f"{start:.3f}", f"{end:.3f}", os.path.basename(path)],
                             capture_output=True, text=True)
        if reg.returncode:
            return self.send_json(500, {"error": (reg.stderr.strip().splitlines() or ["registering failed"])[-1]})
        row = db.execute("SELECT id, name, duration_s FROM takes WHERE file = ?", (fname,)).fetchone()
        if not row:
            return self.send_json(200, {"file": fname})
        self.send_json(200, {"id": row[0], "name": row[1], "duration": row[2]})

    def scene_callouts(self):
        # The scenes screen's Scene callouts, previewed ({dryRun: true}) and
        # then carried out. Each scene gets at most one scene callout: its
        # current name at the top right for its first 3 s (or the whole of a
        # shorter scene), an ordinary item to move, edit or remove, burned in
        # only by Apply. A scene has one when a callout starts at 0 s reaching
        # the top right (x2 >= 0.9, y1 <= 0.25), however wide its box (a long
        # name's starts left of center). Exact copies of one (same text, box
        # and times) are extras, removed keeping the oldest. For each scene:
        # has {text, applied} or null, add (the text it would get) or null,
        # remove (how many extras).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        dry = bool(body.get("dryRun"))
        db = sqlite3.connect(DB)
        overlay.ensure_schema(db)
        plan = []
        for tid, name, dur, file, record in db.execute(
                "SELECT id, name, duration_s, file, overlay_applied FROM takes "
                "ORDER BY position, created_at DESC").fetchall():
            applied = set((record or "").split("|")) if record else set()
            found = db.execute(f"SELECT id, text, x1, y1, x2, y2, t_in, t_out, {APPLIED_ENTRY} FROM overlays "
                               "WHERE take_id = ? AND kind = 'callout' AND t_in < 0.05 AND x2 >= 0.9 AND y1 <= 0.25 "
                               "ORDER BY id", (tid,)).fetchall()
            seen, extras = set(), []
            for r in found:
                key = tuple(r[1:8])
                if key in seen:
                    extras.append(r[0])
                else:
                    seen.add(key)
            keep = next((r for r in found if r[0] not in extras), None)
            plan.append({"id": tid, "name": name, "file": file, "dur": dur, "extras": extras,
                         "has": {"text": keep[1], "applied": keep[8] in applied} if keep else None,
                         "add": None if keep else name})
        if not dry:
            for p in plan:
                for oid in p["extras"]:
                    db.execute("DELETE FROM overlays WHERE id = ?", (oid,))
                if p["add"]:
                    db.execute("INSERT INTO overlays (take_id, kind, text, x1, y1, x2, y2, t_in, t_out, tail, shape) "
                               "VALUES (?, 'callout', ?, ?, ?, ?, ?, 0, ?, NULL, NULL)",
                               (p["id"], p["add"], *scene_callout_box(p["file"], p["add"]), min(3.0, p["dur"] or 3.0)))
            db.commit()
        scenes = [{"id": p["id"], "name": p["name"], "has": p["has"], "add": p["add"], "remove": len(p["extras"])}
                  for p in plan]
        self.send_json(200, {"scenes": scenes, "dryRun": dry, "add": sum(1 for p in plan if p["add"]),
                             "remove": sum(len(p["extras"]) for p in plan)})

    def rename_take(self):
        # {id, name}: a scene's name, as the list shows it (the file keeps its
        # name). Its callouts keep their text.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = " ".join(str(body.get("name") or "").split())
        if not name:
            return self.send_json(400, {"error": "a scene needs a name"})
        db = sqlite3.connect(DB)
        n = db.execute("UPDATE takes SET name = ? WHERE id = ?", (name, body.get("id"))).rowcount
        db.commit()
        if not n:
            return self.send_json(404, {"error": f"no scene with id {body.get('id')}"})
        self.send_json(200, {"id": body.get("id"), "name": name})

    def cut_take(self):
        # {id, start, end}: remove that stretch from a take. The same cut is
        # made to the take's MP4 and to its clean copy (so burned-in callouts
        # need no re-apply), re-encoded with the takes' own settings so Export
        # can still join by stream copy. Overlays after the cut shift earlier,
        # ones overlapping it are clipped, ones inside it go. The originals and
        # a sidecar (the take's row, its overlays) go to media/.trash/ first,
        # for Undo (undo_take).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(DB)
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
        done = self.cut_stretch(db, row, src, dur, start, end, "cut")
        if done:
            # Its row: the seam is now at start; the stretch is start..end of
            # the files the cut's undo record names.
            db.execute("INSERT INTO cuts (take_id, at, seconds, sidecar, src_start, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (row["id"], round(start, 3), round(end - start, 3), done[1], round(start, 3),
                        time.strftime("%Y-%m-%dT%H:%M:%S")))
            db.commit()
            self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "cut", "start": round(start, 2),
                                 "end": round(end, 2), "duration": done[0], "undo": done[1]})

    def cut_stretch(self, db, row, src, dur, start, end, kind):
        # Cut start..end out of the take and its clean copy (apply_take_edit),
        # retiming its overlays, narrations and pauses: for a cut (kind "cut")
        # and for removing a pause, which cuts its held stretch ("unpause").
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

        def pz(at, secs):
            # A cut into a pause's held stretch takes its row (Undo brings it
            # back); one before it moves it earlier.
            if start < at + secs - NARR_EDGE and end > at + NARR_EDGE:
                return None
            return (at - cut, secs) if at >= end - NARR_EDGE else (at, secs)

        def cz(at):
            # A cut containing a seam takes its row (Undo brings it back);
            # one before it moves it earlier.
            if start + NARR_EDGE < at < end - NARR_EDGE:
                return None
            return at - cut if at >= end - NARR_EDGE else at

        return self.apply_take_edit(db, row, src, chain, kind, [round(start, 3), round(end, 3)], retime,
                                    narr=narr, pz=pz, cz=cz)

    def cut_restore(self):
        # {id}: put a cut's removed stretch back at its seam, at any time, from
        # the scene's files as they were before the cut (named by the cut's
        # undo record), in the scene and its clean copy, each from its own.
        # An edit like the others (uncut), so Undo takes it back. Items from
        # the seam on move later and ones across it get longer (items that
        # were inside the stretch went with the cut); a narration across the
        # seam loses its row, as with a pause inside it; pauses and cuts after
        # it move later. remix_take.py can't replay it.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        c = db.execute("SELECT * FROM cuts WHERE id = ?", (body.get("id"),)).fetchone()
        if not c:
            return self.send_json(404, {"error": f"no cut with id {body.get('id')}"})
        row = db.execute("SELECT * FROM takes WHERE id = ?", (c["take_id"],)).fetchone()
        src = os.path.join(ROOT, row["file"]) if row else ""
        if not os.path.isfile(src):
            return self.send_json(409, {"error": "the scene is gone"})
        try:
            with open(os.path.join(TRASH, c["sidecar"])) as f:
                files = json.load(f)["files"]
        except (OSError, ValueError, KeyError):
            return self.send_json(409, {"error": "this cut's undo record is gone (cleaned up?), so its stretch can't be restored"})
        before = {os.path.join(ROOT, rel): os.path.join(TRASH, name) for rel, name in files.items()}
        missing = [b for b in before.values() if not os.path.isfile(b)]
        if missing:
            return self.send_json(409, {"error": f"the footage this cut removed is gone ({os.path.basename(missing[0])})"})
        with DICTATION_LOCK:
            if narration["current"]:
                return self.send_json(409, {"error": "a narration is recording"})
        dur = float(probe(src, "format=duration") or 0)
        q, secs, a = min(max(0.0, c["at"]), dur), c["seconds"], c["src_start"]
        segs = [(0, 0.0, q), (1, a, a + secs), (0, q, dur)]
        segs = [sg for sg in segs if sg[2] - sg[1] > 0.02]
        chain = ""
        for i, (inp, x, y) in enumerate(segs):
            fades = ([f"afade=t=out:st={y - x - 0.01:.3f}:d=0.01"] if i < len(segs) - 1 else []) + \
                    (["afade=t=in:d=0.01"] if i > 0 else [])
            chain += (f"[{inp}:v]trim=start={x:.3f}:end={y:.3f},setpts=PTS-STARTPTS[v{i}];"
                      f"[{inp}:a]atrim=start={x:.3f}:end={y:.3f},asetpts=PTS-STARTPTS"
                      f"{''.join(',' + f for f in fades)}[a{i}];")
        chain += "".join(f"[v{i}][a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=1[v][a]"
        eps = 0.005

        def retime(t_in, t_out):
            if t_in >= q - eps:
                return t_in + secs, t_out + secs
            if t_out > q + eps:
                return t_in, t_out + secs
            return t_in, t_out

        def narr(t_in, t_out):
            if t_in + NARR_EDGE < q < t_out - NARR_EDGE:
                return None
            return (t_in + secs, t_out + secs) if t_in >= q - NARR_EDGE else (t_in, t_out)

        def pz(z_at, z_secs):
            return (z_at + secs, z_secs) if z_at >= q - NARR_EDGE else (z_at, z_secs)

        def cz(at):
            return at + secs if at > q + NARR_EDGE else at

        done = self.apply_take_edit(db, row, src, chain, "uncut", [round(q, 3), round(secs, 3)], retime,
                                    extra=lambda path: [before[path]], narr=narr, pz=pz, cz=cz)
        if done:
            db.execute("DELETE FROM cuts WHERE id = ?", (c["id"],))
            db.commit()
            self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "uncut", "at": round(q, 2),
                                 "seconds": round(secs, 2), "duration": done[0], "undo": done[1]})

    def pause_remove(self):
        # {id}: take an inserted pause back out by cutting its held stretch
        # from the take (cut_stretch), at any time, not only while it is the
        # take's last edit. An edit like the others, so Undo puts it back.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        z = db.execute("SELECT * FROM pauses WHERE id = ?", (body.get("id"),)).fetchone()
        if not z:
            return self.send_json(404, {"error": f"no pause with id {body.get('id')}"})
        row = db.execute("SELECT * FROM takes WHERE id = ?", (z["take_id"],)).fetchone()
        src = os.path.join(ROOT, row["file"]) if row else ""
        if not os.path.isfile(src):
            return self.send_json(409, {"error": "the take is gone"})
        with DICTATION_LOCK:
            if narration["current"]:
                return self.send_json(409, {"error": "a narration is recording"})
        dur = float(probe(src, "format=duration") or 0)
        start, end = z["at"], min(dur, z["at"] + z["seconds"])
        if end - start < 0.02:
            return self.send_json(409, {"error": "the pause is no longer in the take"})
        done = self.cut_stretch(db, row, src, dur, start, end, "unpause")
        if done:
            self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "unpause", "start": round(start, 2),
                                 "end": round(end, 2), "duration": done[0], "undo": done[1]})

    def pause_take(self):
        # {id, at, seconds}: hold the frame at `at` for that long, with silence.
        # Made like a cut (both files, same encoding, same undo). Overlays
        # starting at or after `at` move later; one on the held frame is
        # extended, so it stays up through the pause, as it does in the picture.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(DB)
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

        def pz(z_at, z_secs):
            # A pause inside another's held stretch lengthens it; one before
            # it moves it later.
            if z_at + NARR_EDGE < at < z_at + z_secs - NARR_EDGE:
                return z_at, z_secs + secs
            return (z_at + secs, z_secs) if z_at >= at - NARR_EDGE else (z_at, z_secs)

        def cz(c_at):
            # A pause at or before a seam moves it later.
            return c_at + secs if c_at >= at - NARR_EDGE else c_at

        done = self.apply_take_edit(db, row, src, chain, "pause", [round(at, 3), round(secs, 3)], retime,
                                    narr=narr, pz=pz, cz=cz)
        if done and not db.execute("SELECT 1 FROM pauses WHERE take_id = ? AND at < ? AND at + seconds > ?",
                                   (row["id"], at - NARR_EDGE, at + NARR_EDGE)).fetchone():
            # Its own row, unless it only lengthened a pause already there.
            db.execute("INSERT INTO pauses (take_id, at, seconds, created_at) VALUES (?, ?, ?, ?)",
                       (row["id"], round(at, 3), round(secs, 3), time.strftime("%Y-%m-%dT%H:%M:%S")))
            db.commit()
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
        db = sqlite3.connect(DB)
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
            db = sqlite3.connect(DB)
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
        db = sqlite3.connect(DB)
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

    def apply_take_edit(self, db, row, src, chain, kind, record, retime, extra=(), copy_video=False, narr=None,
                        pz=None, cz=None):
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
            # extra: further inputs, the same for both files, or a function
            # giving each file its own (a restored cut reads the scene's and
            # the clean copy's own before-cut files).
            ex = extra(path) if callable(extra) else extra
            inputs = [a for p in (path, *ex) for a in ("-i", p)]
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
                     "SELECT * FROM narrations WHERE take_id = ? ORDER BY id", (row["id"],))],
                 "pauses": [dict(r) for r in db.execute(
                     "SELECT * FROM pauses WHERE take_id = ? ORDER BY id", (row["id"],))],
                 "cuts": [dict(r) for r in db.execute(
                     "SELECT * FROM cuts WHERE take_id = ? ORDER BY id", (row["id"],))]}
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
        # The take's pauses likewise: pz(at, seconds) gives one's new place and
        # length, or None when this edit cut into its held stretch.
        for z in saved["pauses"]:
            new = pz(z["at"], z["seconds"]) if pz else (z["at"], z["seconds"])
            if new is None:
                db.execute("DELETE FROM pauses WHERE id = ?", (z["id"],))
            elif new != (z["at"], z["seconds"]):
                db.execute("UPDATE pauses SET at = ?, seconds = ? WHERE id = ?",
                           (round(new[0], 3), round(new[1], 3), z["id"]))
        # The take's cuts likewise: cz(at) gives a seam's new place, or None
        # when this edit removed it.
        for c in saved["cuts"]:
            new = cz(c["at"]) if cz else c["at"]
            if new is None:
                db.execute("DELETE FROM cuts WHERE id = ?", (c["id"],))
            elif new != c["at"]:
                db.execute("UPDATE cuts SET at = ? WHERE id = ?", (round(new, 3), c["id"]))
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
        # (Removing a pause is recorded as the cut it is, which remix_take.py
        # can replay.)
        self.mark_take_cut(row["file"], {"cut" if kind == "unpause" else kind: record})
        return new_dur, undo

    @staticmethod
    def mark_take_cut(file, cut):
        # cut = {"cut": [start, end]} or {"pause": [at, seconds]} to record an
        # edit; None to take the last one back.
        meta_path = os.path.join(WORK, "takes",file.replace("-raw.mp4", ".json"))
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
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        return db, db.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone()

    def take_levels_route(self):
        # GET /takes/levels?id=<take>: {binSecs, levels: [dB, ...]} for the
        # take editor's audio strip (take_levels).
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            take_id = int(query.get("id", [""])[0])
        except ValueError:
            return self.send_json(200, {"binSecs": 0, "levels": []})
        _, row = self.take_row(take_id)
        if not row:
            return self.send_json(200, {"binSecs": 0, "levels": []})
        clean = os.path.join(ROOT, ".clean", row["file"])
        path = clean if os.path.isfile(clean) else os.path.join(ROOT, row["file"])
        if not os.path.isfile(path):
            return self.send_json(200, {"binSecs": 0, "levels": []})
        self.send_json(200, take_levels(path))

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
        if "pauses" in snap:
            db.execute("DELETE FROM pauses WHERE take_id = ?", (take_id,))
            for z in snap["pauses"]:
                put("pauses", z)
        if "cuts" in snap:
            db.execute("DELETE FROM cuts WHERE take_id = ?", (take_id,))
            for c in snap["cuts"]:
                put("cuts", c)

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
                         "SELECT * FROM narrations WHERE take_id = ? ORDER BY id", (row["id"],))],
                     "pauses": [dict(r) for r in db.execute(
                         "SELECT * FROM pauses WHERE take_id = ? ORDER BY id", (row["id"],))],
                     "cuts": [dict(r) for r in db.execute(
                         "SELECT * FROM cuts WHERE take_id = ? ORDER BY id", (row["id"],))]}
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

    def log_audition(self):
        # {kind, start, stop, stoppedAt}: the take editor played a stretch and
        # stopped it (playAudition in Main.xmlui). One line per stop, with how
        # far past its end the playhead got, to measure the page's 40 ms Timer.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            stop, stopped = float(body["stop"]), float(body["stoppedAt"])
            print(f"audition: {body.get('kind')} {float(body['start']):.2f}-{stop:.2f} s, "
                  f"stopped at {stopped:.3f} s ({(stopped - stop) * 1000:+.0f} ms)", flush=True)
        except (KeyError, TypeError, ValueError):
            return self.send_json(400, {"error": "need start, stop and stoppedAt"})
        self.send_json(200, {"logged": 1})

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

    def busy(self):
        # What a restart would interrupt, or None. This request is one POST.
        if self.recording():
            return "a take is being recorded"
        if narration["current"]:
            return "a narration is being recorded"
        if dictation["current"]:
            return dictating_where()
        if testing.locked() or exporting.locked() or overlaying.locked() or POSTS["n"] > 1:
            return "Studio is busy (rendering, exporting or saving an edit)"
        return None

    def open_project(self, slug=None):
        # {slug}: make that project the open one. projects/.open is written
        # and the server starts over on it (restart); the pages' event stream
        # reconnects and they refetch. Refused while anything is under way.
        if slug is None:
            slug = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}").get("slug")
        folder = os.path.join(studio_paths.PROJECTS, str(slug))
        if (not isinstance(slug, str) or not slug or os.path.basename(slug) != slug or slug.startswith(".")
                or not os.path.isfile(os.path.join(folder, "project.json"))):
            return self.send_json(404, {"error": f"no project named {slug!r}"})
        if folder == studio_paths.PROJECT:
            return self.send_json(200, {"opened": slug, "restarting": False})
        why = self.busy()
        if why:
            return self.send_json(409, {"error": f"Can't open another project now: {why}"})
        with POSTS_LOCK:
            POSTS["restarting"] = True
        with open(studio_paths.OPEN, "w") as f:
            f.write(slug + "\n")
        self.send_json(200, {"opened": slug, "restarting": True})
        threading.Thread(target=restart).start()

    def delete_stray(self):
        # {slug, path}: move one stray item (a project's, or with slug "" a
        # shared folder's) to the macOS Trash. Only something the listing
        # calls stray right now, so nothing of Studio's can be named. A move
        # within the disk is instant whatever the size, and a wrong click is
        # recoverable until the Trash is emptied; the name gets the time, so
        # it can't land on something already there.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        slug, path = body.get("slug") or "", body.get("path")
        base = os.path.join(studio_paths.PROJECTS, slug) if slug else HERE
        if slug and (os.path.basename(slug) != slug or slug.startswith(".")
                     or not os.path.isfile(os.path.join(base, "project.json"))):
            return self.send_json(404, {"error": f"no project named {slug!r}"})
        item = next((s for s in (project_usage(base)["stray"] if slug else shared_stray()) if s["path"] == path), None)
        if not item:
            return self.send_json(404, {"error": f"{path} isn't a stray item (it may already be gone)"})
        try:
            dest = trash_path(os.path.basename(path))
            os.rename(os.path.join(base, path), dest)
        except OSError as e:
            return self.send_json(500, {"error": f"Couldn't move {path} to the Trash: {e}"})
        print(f"stray: moved {os.path.join(base, path)} to {dest}", flush=True)
        self.send_json(200, {"trashed": path, "bytes": item["bytes"], "as": os.path.basename(dest)})

    def project_folder(self, body):
        # (slug, folder) of an existing project named in a request, or None
        # after answering 404.
        slug = body.get("slug")
        folder = os.path.join(studio_paths.PROJECTS, str(slug))
        if (not isinstance(slug, str) or not slug or os.path.basename(slug) != slug or slug.startswith(".")
                or not os.path.isfile(os.path.join(folder, "project.json"))):
            self.send_json(404, {"error": f"no project named {slug!r}"})
            return None
        return slug, folder

    def set_safekeeping(self):
        # {slug, location, create}: where this project's safekeeping copies
        # go, saved in its project.json. A folder, written to as
        # <location>/<slug>/; not inside Studio's own projects folder, where
        # a copy would be counted as stray. "" forgets it. A missing folder
        # isn't an error: the answer says so ({missing, location}) without
        # saving, so the page can ask, and create: true then makes it. Only
        # the last folder of the path is made, so a mistyped parent is
        # refused rather than made into a new tree.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        found = self.project_folder(body)
        if not found:
            return
        slug, folder = found
        loc = os.path.expanduser(str(body.get("location") or "").strip())
        if loc:
            loc = os.path.abspath(loc)
            real, projects = os.path.realpath(loc), os.path.realpath(studio_paths.PROJECTS)
            if real == projects or real.startswith(projects + os.sep):
                return self.send_json(400, {"error": "Safekeeping can't be inside Studio's projects folder"})
            if not os.path.isdir(loc):
                parent = os.path.dirname(loc)
                if not os.path.isdir(parent):
                    return self.send_json(400, {"error": f"There is no folder at {parent} to make it in"})
                if not body.get("create"):
                    return self.send_json(200, {"slug": slug, "location": loc, "missing": True})
                try:
                    os.mkdir(loc)
                except OSError as e:
                    return self.send_json(400, {"error": f"Couldn't make {loc}: {e}"})
                print(f"safekeeping: made {loc} for {slug}", flush=True)
            real = os.path.realpath(loc)
            if real == projects or real.startswith(projects + os.sep):
                return self.send_json(400, {"error": "Safekeeping can't be inside Studio's projects folder"})
            if not os.access(loc, os.W_OK):
                return self.send_json(400, {"error": f"Studio can't write to {loc}"})
        info = read_project(folder)
        if loc != info.get("safekeeping"):
            # The last copy was to the old location (its copies stay there,
            # untouched); a new location, or none, starts its record afresh.
            info.pop("last_copy", None)
        info["safekeeping"] = loc
        write_project(folder, info)
        self.send_json(200, {"slug": slug, "location": loc})

    def reorder_projects(self):
        # {slugs}: the Projects page's drag order, saved to projects/.order
        # (list_projects sorts by it). Only existing projects are kept.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        slugs = [s for s in body.get("slugs") or [] if isinstance(s, str) and os.path.basename(s) == s
                 and not s.startswith(".") and os.path.isfile(os.path.join(studio_paths.PROJECTS, s, "project.json"))]
        write_order(slugs)
        self.send_json(200, {"order": slugs})

    def delete_project(self):
        # {slug}: move the whole project folder to the system Trash, so it
        # can be dragged back into projects/. It keeps its folder name there
        # (projects/<slug> back in place is the same project); only when the
        # Trash already holds that name does trash_path add the time. It
        # leaves the drag order. Not the open project: Studio is running on
        # it. Its safekeeping copy, wherever it is, is left alone.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        found = self.project_folder(body)
        if not found:
            return
        slug, folder = found
        if folder == studio_paths.PROJECT:
            return self.send_json(409, {"error": "That's the open project: open another one first"})
        if SAFE_LOCK.locked():
            return self.send_json(409, {"error": "A safekeeping copy is running"})
        try:
            dest = trash_path(slug)  # refuses off macOS
            plain = os.path.join(os.path.dirname(dest), slug)
            if not os.path.lexists(plain):
                dest = plain
            os.rename(folder, dest)
        except OSError as e:
            return self.send_json(500, {"error": f"Couldn't move {slug} to the Trash: {e}"})
        write_order([s for s in read_order() if s != slug])
        print(f"projects: moved {folder} to {dest}", flush=True)
        self.send_json(200, {"deleted": slug, "as": os.path.basename(dest)})

    def clear_safekeeping(self):
        # {slug}: a fresh start for the project's safekeeping. Its copy,
        # <location>/<slug>/, goes to the system Trash (trash_path; a
        # mistake is recoverable until the Trash is emptied), the record of
        # the last copy is forgotten, and the location stays, so the next
        # copy copies everything again.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        found = self.project_folder(body)
        if not found:
            return
        slug, folder = found
        info = read_project(folder)
        loc = info.get("safekeeping")
        dest = os.path.join(loc, slug) if loc else ""
        if not dest or not os.path.isdir(dest):
            return self.send_json(404, {"error": "There is no safekeeping copy to clear"})
        if not SAFE_LOCK.acquire(blocking=False):
            return self.send_json(409, {"error": "A copy is running"})
        try:
            to = trash_path(f"Bram Studio - {info.get('name') or slug} - safekeeping copy")
            os.rename(dest, to)
        except OSError as e:
            return self.send_json(500, {"error": f"Couldn't move {dest} to the Trash: {e}"})
        finally:
            SAFE_LOCK.release()
        info.pop("last_copy", None)
        write_project(folder, info)
        print(f"safekeeping: cleared {dest} to {to}", flush=True)
        self.send_json(200, {"cleared": dest, "as": os.path.basename(to)})

    def copy_project(self):
        # {slug}: copy the project to its safekeeping location, as
        # <location>/<slug>/ laid out like the project folder, so bringing it
        # back would be a plain copy the other way. A file already there with
        # the same size and time is skipped; nothing there is ever deleted,
        # and files there that the project no longer has are counted. The
        # database is taken with SQLite's backup, so it is consistent.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        found = self.project_folder(body)
        if not found:
            return
        slug, folder = found
        info = read_project(folder)
        loc = info.get("safekeeping")
        if not loc:
            return self.send_json(409, {"error": "Set a safekeeping location first"})
        if not os.path.isdir(loc):
            return self.send_json(409, {"error": f"The safekeeping location isn't there: {loc}"})
        why = self.busy() if folder == studio_paths.PROJECT else None
        if why:
            return self.send_json(409, {"error": f"Can't copy the open project now: {why}"})
        if not SAFE_LOCK.acquire(blocking=False):
            return self.send_json(409, {"error": "A copy is already running"})
        try:
            started, dest = time.time(), os.path.join(loc, slug)
            files = keep_files(folder)
            copied = skipped = size = 0
            for src, rel in files:
                to = os.path.join(dest, rel)
                size += os.path.getsize(src)
                if same_file(src, to):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(to), exist_ok=True)
                tmp = os.path.join(os.path.dirname(to), f".{os.path.basename(to)}.part")
                shutil.copy2(src, tmp)
                os.replace(tmp, to)
                copied += 1
            os.makedirs(dest, exist_ok=True)
            tmp = os.path.join(dest, ".studio.db.part")
            src_db, dst_db = sqlite3.connect(os.path.join(folder, "studio.db")), sqlite3.connect(tmp)
            try:
                src_db.backup(dst_db)
            finally:
                dst_db.close()
                src_db.close()
            os.replace(tmp, os.path.join(dest, "studio.db"))
            size += os.path.getsize(os.path.join(dest, "studio.db"))
            kept = {rel for _, rel in files} | {"studio.db", "project.json"}
            extra = sum(1 for root, _, names in os.walk(dest) for n in names
                        if not n.startswith(".") and os.path.relpath(os.path.join(root, n), dest) not in kept)
            info["last_copy"] = {"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(started)), "at_s": started,
                                 "to": dest, "files": len(files) + 2, "bytes": size, "copied": copied + 2,
                                 "skipped": skipped, "extra": extra, "seconds": round(time.time() - started, 1)}
            write_project(folder, info)
            shutil.copy2(os.path.join(folder, "project.json"), os.path.join(dest, "project.json"))
        except (OSError, sqlite3.Error) as e:
            return self.send_json(500, {"error": f"The copy didn't finish: {e}"})
        finally:
            SAFE_LOCK.release()
        print(f"safekeeping: {slug} to {dest}: {copied} copied, {skipped} unchanged, {extra} there not in the "
              f"project, {info['last_copy']['seconds']}s", flush=True)
        self.send_json(200, info["last_copy"])

    def clean_project(self):
        # {slug, what, days, dry}: a clean-up button of the Projects page.
        # what is a category with buttons (CLEAN): "discarded", "deleted"
        # (those deleted at least `days` days ago; 0 for all) or "undo".
        # dry: only say what would go, {files, bytes}, for the confirmation.
        # Otherwise everything goes to the system Trash as one folder named
        # for the project and the category, laid out as it was in the
        # project, so a mistake can be put back by hand. Nothing in the takes
        # list, its re-mix ingredients or the exports is ever among it.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        slug, what, dry = body.get("slug"), body.get("what"), bool(body.get("dry"))
        folder = os.path.join(studio_paths.PROJECTS, str(slug))
        if (not isinstance(slug, str) or not slug or os.path.basename(slug) != slug or slug.startswith(".")
                or not os.path.isfile(os.path.join(folder, "project.json"))):
            return self.send_json(404, {"error": f"no project named {slug!r}"})
        try:
            days = max(0.0, float(body.get("days") or 0))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "days must be a number"})
        if what not in CLEAN:
            return self.send_json(400, {"error": f"nothing to clean called {what!r}"})
        # The open project's recordings and history are in use while a take
        # is being recorded or rendered, or an edit is being saved.
        why = self.busy() if folder == studio_paths.PROJECT else None
        if why and not dry:
            return self.send_json(409, {"error": f"Can't clean up the open project now: {why}"})
        with EDIT_LOCK:
            found = clean_paths(folder, what, days)
            sizes = [tree_size(p) for p in found]
            total = {"files": sum(n for _, n in sizes), "bytes": sum(b for b, _ in sizes)}
            if dry or not found:
                summary, tree = clean_summary(folder, what, found) if dry and found else ("", [])
                return self.send_json(200, {**total, "dry": dry, "summary": summary, "tree": tree})
            try:
                with open(os.path.join(folder, "project.json")) as f:
                    name = json.load(f).get("name") or slug
                dest = trash_path(f"Bram Studio - {name} - {dict((k, l) for k, l, _ in USAGE)[what]}")
                for p in found:
                    to = os.path.join(dest, os.path.relpath(p, folder))
                    os.makedirs(os.path.dirname(to), exist_ok=True)
                    os.rename(p, to)
            except (OSError, ValueError) as e:
                return self.send_json(500, {"error": f"Couldn't move it all to the Trash: {e}"})
        print(f"clean: moved {total['files']} files ({total['bytes']} bytes) of {slug} {what} to {dest}", flush=True)
        if folder == studio_paths.PROJECT:
            notify("takes")  # an open History tab refetches
        self.send_json(200, {**total, "as": os.path.basename(dest)})

    def new_project(self):
        # {name}: a new, empty project folder (studio_paths.py), then open it.
        # The folder is named from the name: lowercase, dashes for the rest.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = str(body.get("name") or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not slug:
            return self.send_json(400, {"error": "Give the project a name with a letter or digit in it"})
        folder = os.path.join(studio_paths.PROJECTS, slug)
        if os.path.exists(folder):
            return self.send_json(409, {"error": f"There is already a project folder named {slug}"})
        why = self.busy()
        if why:
            return self.send_json(409, {"error": f"Can't open a new project now: {why}"})
        os.makedirs(os.path.join(folder, "media"))
        os.makedirs(os.path.join(folder, "work"))
        with open(os.path.join(folder, "project.json"), "w") as f:
            json.dump({"name": name}, f, indent=1)
        with sqlite3.connect(os.path.join(folder, "studio.db")) as db:
            db.executescript(open(os.path.join(HERE, "schema.sql")).read())
        self.open_project(slug)

    def query(self):
        # The pages' DataSource dataType="sql" requests, answered from the
        # open project's database: {sql, params} (or bare SQL) in, a JSON
        # array of column-keyed rows out. The same contract as Bram's /query,
        # which serves only the one database .bram.json names (the repo's,
        # with the Audio bench's voice_tests). Read-only at the engine: opened
        # mode=ro with query_only on, so a page can't change project data.
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = raw.decode("utf-8", "replace")
        sql, params = (body, []) if isinstance(body, str) else (
            (body.get("sql"), body.get("params") or []) if isinstance(body, dict) else (None, []))
        if not isinstance(sql, str) or not sql.strip() or not isinstance(params, list):
            return self.send_json(400, {"error": "expected {sql, params}"})
        try:
            db = sqlite3.connect(f"file:{urllib.parse.quote(DB)}?mode=ro", uri=True)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                rows = [dict(r) for r in db.execute(sql, params)]
            finally:
                db.close()
        except sqlite3.Error as e:
            return self.send_json(400, {"error": str(e)})
        self.send_json(200, rows)

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


os.makedirs(ROOT, exist_ok=True)
# The callouts query reads overlays, so it must exist before the page asks.
with sqlite3.connect(DB) as _db:
    overlay.ensure_schema(_db)
    _db.execute(NARRATIONS_SQL)
    _db.execute(PAUSES_SQL)
    _db.execute(CUTS_SQL)
recorder = adopt_running_take()
backfill_studio_files()
threading.Thread(target=watch_changes, daemon=True).start()
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
