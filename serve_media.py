#!/usr/bin/env python3
"""Serve the open project's media/ with video content types and HTTP byte ranges.

    python3 serve_media.py [port]      (default 8765, or the next free port;
                                        the choice is in studio-port.json)

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
on it (refused while a narration, render, export or edit is under way).

GET /sources lists the movies in sources/ and on the Desktop (newest first)
and /sources/<name> streams one;
GET /devices lists the audio inputs and
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
with where it goes, for the take player's PointerLayer anchors (#3922).
Export burns each changed scene's items in with overlay.py (apply_scene).
GET /events is a server-sent-events stream: {"changed": "takes"|"sources"|
"exports"|"projects"} whenever watch_changes() sees one of them change, so the
page refetches then instead of polling.
The Speakers page: POST /transcripts/start {source, prompt, replace} runs
whisper-cli over a source in the background (GET /transcripts/status?source=,
and {"transcribing": source, done_s, duration_s} on /events while it runs);
POST /speakers/add {name}, /speakers/rename {id, name}, /speakers/delete {id};
POST /segments/speaker {speaker_id, ids | source, t0, t1} labels segments.
Each announces {"changed": "transcripts"|"speakers"} on /events.
Editing by text: GET /transcripts/text?source= is the kept words as text;
POST /transcripts/edit {source, text} saves an edited copy (deleted words are
cut, words changed one for one are corrections), /transcripts/undo {source}
takes the last change back, /transcripts/restore {source, ids} puts cut words
back; GET /transcripts/cuts?source= is the edit list, cut spans snapped to
quiet and the kept intervals on the edited timeline. Each change announces
{"changed": "words"}. Cut edges are placed by the sound (align.py, a CTC
model under sherpa-onnx), cached in word_edges. POST /transcripts/check
{source, t0, t1} renders the edit, transcribes it and flags joins where the
words heard differ from the words kept ({"checking": source, done} on
/events).
"""
import collections
import hashlib
import html as html_lib
import io
import math
import http.server
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse

from origin_gate import host_allowed, origin_allowed  # server-origin-host-gate
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import overlay
import studio_paths
import voicetest

HERE = studio_paths.REPO  # the scripts, record.log
# The open project (studio_paths.py): its takes and exports, its database and
# its render ingredients. The scripts launched from here inherit it.
ROOT, DB, WORK = studio_paths.MEDIA, studio_paths.DB, studio_paths.WORK
CONFIG = os.path.join(HERE, "config.json")  # the page's XMLUI config: appGlobals.xsVerbose is trace capture
SHARED_DB = os.path.join(HERE, "studio.db")  # the repo's: the Audio bench's voice_tests (voicetest.py)
PROJECT_GIVEN = bool(os.environ.get("STUDIO_PROJECT"))  # before the next line sets it for the scripts
os.environ["STUDIO_PROJECT"] = studio_paths.PROJECT
# A fresh checkout serves with no project at all until New project makes the
# first one (ensure_open_project sets this; no-auto-first-project). The
# dispatcher turns project routes away while it's set, and startup leaves the
# repo's own studio.db and media/ untouched.
NO_PROJECT = False
testing = threading.Lock()  # held while voicetest.py has the mic
SOURCES = studio_paths.SOURCES  # source movies, usually symlinks
DESKTOP = os.path.expanduser("~/Desktop")  # where recordings usually land


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


# One item's entry as apply_scene records it (overlay.APPLIED_SQL joins these
# with |): made with the same SQL so numbers format identically, for telling
# whether an item is in its scene's video as it is now.
APPLIED_ENTRY = ("id || char(58) || text || char(58) || x1 || char(58) || y1 || char(58) || x2 || char(58) || y2 "
                 "|| char(58) || t_in || char(58) || t_out || char(58) || ifnull(tail, char(45)) || char(58) "
                 "|| ifnull(shape, char(45))")


def apply_scene(take_id):
    # Burn a scene's items into its file (overlay.py render), from its clean
    # copy: Export's first step for each scene whose items changed (there is
    # no Apply button any more). It rewrites the file without being an edit, so the
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
USAGE = (("takes", "Takes", "the takes, their clean copies, their lossless audio, replaced narration audio, "
          "previews"),
         ("remix", "Re-mix ingredients", "sessions and tracks of the takes in the list"),
         ("exports", "Exports", "Final videos and other exported videos (the Exports tab)"),
         ("transcripts", "Transcripts", "whisper's transcripts and the audio it heard, one folder per source; "
          "Transcribe remakes them"),
         ("logs", "Logs", "logs Studio wrote, kept for tracing what happened"),
         ("undo", "Undo history", "what the History tab can still restore"),
         ("deleted", "Deleted takes", "takes you deleted that Studio can still restore, with their recordings"),
         ("discarded", "Discarded", "what nothing in Studio uses or can restore: deleted exports, takes deleted "
          "before Undo delete existed, abandoned recordings, render leftovers"),
         ("stray", "Scratch files", "Listed below, expand for details."))
MOVIES = (".mp4", ".mov", ".m4v")
# In media/.trash/: a step of a take's edit history (apply_take_edit,
# record_annot_step, remix_take.py).
HISTORY_FILE = re.compile(r"(.+?)(-clean)?-(before|after)-(cut|uncut|pause|unpause|narrate|unnarrate|annot|remix)-\d{8}-\d{6}\.\w+$")


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


def tree_size(path, seen=None):
    # (bytes on disk, files) under a file or a folder; links aren't followed.
    # seen: a set shared across calls; a file with other hard links
    # (st_nlink > 1) is counted only the first time its (device, inode) turns
    # up, so a project's total doesn't count one file under two names twice.
    def count(st):
        if seen is not None and st.st_nlink > 1:
            key = (st.st_dev, st.st_ino)
            if key in seen:
                return 0, 0
            seen.add(key)
        return disk_size(st), 1

    if os.path.islink(path) or not os.path.isdir(path):
        return count(os.lstat(path))
    size = files = 0
    for root, _, names in os.walk(path):
        for n in names:
            try:
                b, c = count(os.lstat(os.path.join(root, n)))
                size += b
                files += c
            except OSError:
                pass
    return size, files


def scratch_details(path):
    # What the Manage page says about a scratch item: {files, modified, sample}.
    # modified: the newest modification time in it (epoch seconds). sample, for
    # a folder: up to 5 names directly inside, largest first.
    newest = 0
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        pass
    is_dir = os.path.isdir(path) and not os.path.islink(path)
    files = 1
    if is_dir:
        files = 0
        for root, _, names in os.walk(path):
            for n in names:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, n)).st_mtime)
                    files += 1
                except OSError:
                    pass
    out = {"files": files, "modified": newest}
    if is_dir:
        try:
            kids = [(tree_size(os.path.join(path, n))[0], n) for n in os.listdir(path) if n != ".DS_Store"]
        except OSError:
            kids = []
        kids.sort(key=lambda k: (-k[0], k[1]))
        out["sample"] = [n for _, n in kids[:5]]
    return out


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

    seen_links = set()  # hard-linked files counted once across the whole project

    def add(kind, path):
        b, n = tree_size(path, seen_links)
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
                          "folder": os.path.isdir(path) and not os.path.islink(path),
                          **scratch_details(path)})

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
        elif n == ".audio":
            # Lossless audio (<stem>.flac and .json): a live take's, a
            # deleted one's (Undo delete brings it back with it), or leftover.
            for c in entries(p):
                take = os.path.splitext(c)[0] + ".mp4"
                add("takes" if take in live_files else "deleted" if take in deleted_files - live_files
                    else "discarded", os.path.join(p, c))
        elif n == ".trash":
            for c in entries(p):
                m = HISTORY_FILE.match(c)
                add("undo" if m and m.group(1) in live_media else "deleted" if c in restorable else "discarded",
                    os.path.join(p, c))
        elif n == "exports":
            for c in entries(p):
                cp = os.path.join(p, c)
                # finals/: a podcast's Final video versions (finals_dir), what
                # the Final video tab lists (manage-podcast-files-not-scratch).
                ours = (os.path.isfile(cp) and (c.startswith(".") or c.lower().endswith(".mp4"))) or \
                    (c == "finals" and os.path.isdir(cp) and not os.path.islink(cp))
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
        elif n == "transcripts" and os.path.isdir(p):
            # A podcast's transcripts (transcript_folder): whisper's output
            # and the audio it heard, per source. Studio's, and not offered
            # for clean-up (manage-podcast-files-not-scratch).
            add("transcripts", p)
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
    # A podcast has no takes: its first row is the project's own files.
    info = read_project(folder) or {}
    labels = {k: (label, about) for k, label, about in USAGE}
    if info.get("genre") == "podcast":
        labels["takes"] = ("Project files", "this project's database (the transcript, your edits, speakers) "
                           "and previews Studio rebuilds as needed")
        # A podcast has no Exports tab (tabs-by-genre-exports-final-video).
        labels["exports"] = ("Exports", "Final video versions (the Final video tab)")
    return {"bytes": sum(size.values()),
            "categories": [{"key": k, "label": labels[k][0], "about": labels[k][1], "bytes": size[k],
                            "files": count[k]} for k, _, _ in USAGE],
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
                      "folder": os.path.isdir(path) and not os.path.islink(path), **scratch_details(path)})

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


META_FIELDS = ("title", "date", "summary")


def project_meta(info):
    # The project's metadata (Settings › Metadata), {title, date, summary},
    # each a string, '' when unset: what an export says the recording is
    # (final-transcript-html-export).
    # showTranscript: whether the HTML transcript opens with its transcript
    # shown (false, the default: just the video and the heading).
    meta = (info or {}).get("meta") or {}
    return {**{k: meta.get(k) if isinstance(meta.get(k), str) else "" for k in META_FIELDS},
            "showTranscript": meta.get("showTranscript") is True}


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


# Studio's own settings (the Settings page): projects/.studio-settings.json,
# with this machine's projects (projects/ is git-ignored). The first is
# defaultSafekeeping, the safekeeping folder of any project without its own.
SETTINGS = os.path.join(studio_paths.PROJECTS, ".studio-settings.json")


def read_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# The Podcast page's settings, with their defaults: durations and sizes that
# are a matter of taste or of the recording, so they're Settings, not
# constants. Each is (default, minimum, maximum).
PODCAST_SETTINGS = {
    "pauseMs": (80, 20, 500),  # quiet must last this long to count as a pause between words (align.py)
    "fadeMs": (10, 0, 100),  # the fade at each join when an edit is played
    "paragraphPauseS": (0.5, 0.1, 5.0),  # a sentence ending in a pause this long ends a paragraph
    "paragraphWords": (120, 20, 1000),  # ...and so does any sentence once a paragraph has this many words
    # Voiceprints (voiceprint.py): how much audio each voiceprint is taken
    # from and how far apart, the shortest run of words left as its own
    # turn, and how much tagged speech a speaker needs before matching.
    "vpWindowS": (1.5, 0.5, 5.0),
    "vpStepS": (0.5, 0.1, 2.0),
    "vpMinTurnS": (1.0, 0.0, 10.0),
    "vpMinTaggedS": (10.0, 2.0, 120.0),
    # How far a change of speaker may move to land on a pause or a sentence
    # end (voiceprint.py's edge pass); 0 turns the pass off.
    "vpEdgeS": (2.0, 0.0, 5.0),
}


# Word lists, likewise Settings: what counts as a tic is a matter of taste.
# Comma-separated; matched on a word's letters, ignoring case and punctuation.
PODCAST_WORD_SETTINGS = {
    "fillers": "um, umm, uh, uhm, er, ah, hmm, mm",  # cut wherever they're kept (elide_tics)
    "repeatsToKeep": "yeah, no, very, blah, bye",  # said twice on purpose: not cut as stutters
}


def podcast_settings():
    saved = read_settings().get("podcast", {})
    out = {k: str(saved.get(k, default)) for k, default in PODCAST_WORD_SETTINGS.items()}
    for k, (default, lo, hi) in PODCAST_SETTINGS.items():
        try:
            v = type(default)(saved.get(k, default))
        except (TypeError, ValueError):
            v = default
        out[k] = min(max(v, lo), hi)
    return out


def write_settings(settings):
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=1)
    os.replace(tmp, SETTINGS)


def safekeeping_location(info):
    # (folder, whether it's the default): the project's own location, else
    # the Settings page's defaultSafekeeping. Copies go to <folder>/<slug>.
    own = info.get("safekeeping") or ""
    if own:
        return own, False
    default = read_settings().get("defaultSafekeeping") or ""
    return default, bool(default)


def check_safekeeping_folder(loc, create):
    # A safekeeping folder as typed: ~ expanded, made absolute. Returns
    # (folder, error, missing). Not inside Studio's projects folder, where a
    # copy would be counted as stray. A missing folder isn't an error: with
    # create it's made (only its last part, so a mistyped parent is refused
    # rather than made into a new tree); without, missing says so, so the
    # page can ask.
    loc = os.path.abspath(os.path.expanduser(loc))
    projects = os.path.realpath(studio_paths.PROJECTS)
    inside = lambda p: os.path.realpath(p) == projects or os.path.realpath(p).startswith(projects + os.sep)
    if inside(loc):
        return loc, "Safekeeping can't be inside Studio's projects folder", False
    if not os.path.isdir(loc):
        parent = os.path.dirname(loc)
        if not os.path.isdir(parent):
            return loc, f"There is no folder at {parent} to make it in", False
        if not create:
            return loc, None, True
        try:
            os.mkdir(loc)
        except OSError as e:
            return loc, f"Couldn't make {loc}: {e}", False
        print(f"safekeeping: made {loc}", flush=True)
    if inside(loc):
        return loc, "Safekeeping can't be inside Studio's projects folder", False
    if not os.access(loc, os.W_OK):
        return loc, f"Studio can't write to {loc}", False
    return loc, None, False


def safekeeping_state(folder, info, usage):
    # What the Projects page shows: the location, the last copy, and whether
    # the project has changed since (a kept file newer than the copy, or
    # one not copied yet).
    last = info.get("last_copy")
    loc, is_default = safekeeping_location(info)
    # A copy made to another location (the default changed since) isn't this
    # location's copy: shown as not copied yet.
    if last and last.get("to") and loc and last["to"] != os.path.join(loc, os.path.basename(folder)):
        last = None
    changed = None
    if last:
        at = last.get("at_s", 0)
        changed = any(os.path.getmtime(p) > at for p, _ in keep_files(folder, usage)) or \
            os.path.getmtime(os.path.join(folder, "studio.db")) > at
    copy_path = os.path.join(loc, os.path.basename(folder)) if loc else ""
    return {"location": loc, "default": is_default, "exists": bool(loc) and os.path.isdir(loc), "last": last,
            "changed_since": changed,
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
    return {"slug": slug, "name": name, "genre": info.get("genre", ""), "sources": info.get("sources", []),
            "takes": takes, "seconds": round(seconds, 1), "changed": changed,
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
    # The open project's own genre and sources, read from its project.json: it
    # needn't be in the list (STUDIO_PROJECT can point outside projects/).
    mine = read_project(studio_paths.PROJECT) or {}
    return {"open": current and current["slug"], "name": current["name"] if current else "", "projects": projects,
            "genre": mine.get("genre", ""), "sources": mine.get("sources", []),
            "meta": project_meta(mine),
            "shared_stray": shared, "stray_count": len(shared) + sum(len(p["stray"]) for p in projects),
            "folder": studio_paths.PROJECTS}


GENRES = ("screencast", "podcast")


def create_project_folder(folder, name, genre="screencast", sources=()):
    # A new, empty project (studio_paths.py): media/, work/, project.json and
    # a studio.db from schema.sql. The genre is chosen here, once; no route
    # changes it later.
    os.makedirs(os.path.join(folder, "media"))
    os.makedirs(os.path.join(folder, "work"))
    with open(os.path.join(folder, "project.json"), "w") as f:
        json.dump({"name": name, "genre": genre, "sources": list(sources)}, f, indent=1)
    with sqlite3.connect(os.path.join(folder, "studio.db")) as db:
        db.executescript(open(os.path.join(HERE, "schema.sql")).read())


def migrate_genres(folders):
    # Projects made before genre and sources were recorded in project.json:
    # work both out from the database, once (write_project saves them, so the
    # next start finds nothing missing). Sources are the files its takes and
    # transcripts came from, in first-use order. A project with transcripts
    # and no takes is a podcast; anything else is a screencast. A folder
    # with no project.json isn't a project and is left alone.
    for folder in folders:
        info = read_project(folder)
        if info is None or ("genre" in info and "sources" in info):
            continue
        used, takes, transcripts = [], 0, 0
        try:
            db = sqlite3.connect(f"file:{urllib.parse.quote(os.path.join(folder, 'studio.db'))}?mode=ro", uri=True)
        except sqlite3.Error:
            db = None
        if db:
            try:
                # transcripts doesn't exist in a database older than the Podcast page.
                for table in ("takes", "transcripts"):
                    try:
                        rows = [r[0] for r in db.execute(f"SELECT source FROM {table} ORDER BY rowid") if r[0]]
                    except sqlite3.Error:
                        rows = []
                    if table == "takes":
                        takes = len(rows)
                    else:
                        transcripts = len(rows)
                    for r in rows:
                        if os.path.basename(r) not in used:
                            used.append(os.path.basename(r))
            finally:
                db.close()
        info.setdefault("genre", "podcast" if transcripts and not takes else "screencast")
        info.setdefault("sources", used)
        write_project(folder, info)
        print(f"startup: {info['name']!r} is a {info['genre']} on {info['sources']}", flush=True)


def migrate_all_genres():
    try:
        slugs = [s for s in os.listdir(studio_paths.PROJECTS) if not s.startswith(".")]
    except OSError:
        slugs = []
    # The repo itself is the project when there is no projects/ (studio_paths.py).
    migrate_genres([os.path.join(studio_paths.PROJECTS, s) for s in sorted(slugs)] + [studio_paths.REPO])


PENDING_DELETE = os.path.join(studio_paths.PROJECTS, ".delete-pending")


def finish_pending_delete():
    # At startup: a project deleted from its own Manage page
    # (delete_open_project) goes to the Trash now that it isn't open, under
    # its own name as delete_project does, so dragging it back restores it.
    try:
        slug = open(PENDING_DELETE).read().strip()
    except OSError:
        return
    folder = os.path.join(studio_paths.PROJECTS, slug)
    try:
        if slug and os.path.basename(slug) == slug and folder != studio_paths.PROJECT and os.path.isdir(folder):
            dest = trash_path(slug)
            plain = os.path.join(os.path.dirname(dest), slug)
            if not os.path.lexists(plain):
                dest = plain
            os.rename(folder, dest)
            write_order([s for s in read_order() if s != slug])
            print(f"project deleted slug={slug} (pending) to={dest}", flush=True)
        os.remove(PENDING_DELETE)
    except OSError as e:
        print(f"project delete failed slug={slug} (pending) error={e}", flush=True)


def ensure_open_project():
    # At startup, before anything opens the database: make sure a project
    # under projects/ is open. With STUDIO_PROJECT given (PROJECT_GIVEN: this
    # module sets it for the scripts it launches), or projects/.open
    # naming a project, nothing to do. Otherwise studio_paths fell back to
    # the repo itself, the layout from before projects; a fresh checkout's
    # studio.db has no tables there, and the server crashed on "no such
    # table: takes" (2026-10-05). So: open the most recently changed project
    # if there are any; leave an old, unmigrated repo (its studio.db holds
    # takes) as it is, since migrate_project.py moves that; else serve with
    # no project (NO_PROJECT) until New project makes the first. Opening one
    # restarts the server, as Open on the Projects page does, so every path
    # here and every script it launches agree.
    if PROJECT_GIVEN or studio_paths.PROJECT != studio_paths.REPO:
        return
    projects = studio_paths.PROJECTS
    found = []
    if os.path.isdir(projects):
        for n in os.listdir(projects):
            folder = os.path.join(projects, n)
            if not n.startswith(".") and os.path.isfile(os.path.join(folder, "studio.db")):
                found.append((os.path.getmtime(folder), n))
    if found:
        slug = max(found)[1]
        print(f"startup: no open project; opening {slug}, the most recently changed", flush=True)
    else:
        try:
            with sqlite3.connect(f"file:{urllib.parse.quote(os.path.join(HERE, 'studio.db'))}?mode=ro", uri=True) as db:
                legacy = db.execute("SELECT count(*) FROM takes").fetchone()[0] > 0
        except sqlite3.Error:
            legacy = False
        if legacy:
            print("startup: no open project; the repo's own studio.db holds takes, so it stays the "
                  "project (migrate_project.py moves it into projects/)", flush=True)
            return
        # No projects and not a legacy repo: serve with none, rather than
        # invent one ("let's not make a dumb project — the New project button
        # will suffice", 2026-11-15). /projects/new makes the first project
        # and restarts into it; until then the dispatcher's no_project_ok
        # gate turns project routes away.
        global NO_PROJECT
        NO_PROJECT = True
        print("startup: no projects; serving without one until New project makes the first", flush=True)
        return
    os.makedirs(projects, exist_ok=True)
    with open(studio_paths.OPEN, "w") as f:
        f.write(slug + "\n")
    os.environ.pop("STUDIO_PROJECT", None)  # projects/.open decides now, as in restart()
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])


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
    # What each of the page's lists is built from, cheaply:
    # studio.db (takes, callouts; register.py writes it too), the two
    # source folders, the exports folder and the project folders. A change
    # means "refetch that".
    def mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None
    return {"takes": takes_fingerprint(),
            "sources": (mtime(SOURCES), mtime(DESKTOP)),
            "exports": exports_fingerprint(),
            "projects": projects_fingerprint(mtime)}


# The columns of the takes the page's scene list shows (Main.xmlui's takes
# query), plus their order. Only a change to these is "takes changed": the
# editor's own writes to studio.db (callouts, narrations, undo records)
# refresh what they touch from their handlers, and announcing them made the
# page refetch its lists and redraw itself six times per edit (~1 s of a
# 2.1 s nudge; render trace, xmlui-org/xmlui#3942).
TAKES_LISTED = ("SELECT id, name, created_at, duration_s, url, file, source, src_end, warning, position"
                " FROM takes ORDER BY id")


def takes_fingerprint():
    if not os.path.isfile(DB):
        return None
    db = sqlite3.connect(f"file:{urllib.parse.quote(DB)}?mode=ro", uri=True)
    try:
        return hashlib.sha1(repr(db.execute(TAKES_LISTED).fetchall()).encode()).hexdigest()
    finally:
        db.close()


def projects_fingerprint(mtime):
    # For the Projects page, which measures every project folder (too slow to
    # do every second): the modification times of projects/, the drag order,
    # and each project's folder and top-level folders. A file or folder
    # dropped in from outside, a project added or removed, or a new order
    # changes one of them; changes inside the open project's database and
    # exports arrive as "takes" and "exports" too. So the open project's
    # studio.db, and its folder (whose time changes with SQLite's journal
    # file on every write), aren't watched here.
    found = [mtime(studio_paths.PROJECTS), mtime(ORDER)]
    try:
        slugs = sorted(s for s in os.listdir(studio_paths.PROJECTS) if not s.startswith("."))
    except OSError:
        slugs = []
    open_db = os.path.realpath(DB)
    for slug in slugs:
        base = os.path.join(studio_paths.PROJECTS, slug)
        parts = [("project.json",), ("media",), ("media", "exports"), ("media", ".trash"),
                 ("media", ".clean"), ("work",), ("work", "sessions"), ("work", "takes")]
        if os.path.realpath(os.path.join(base, "studio.db")) != open_db:
            parts += [(), ("studio.db",)]
        found += [mtime(os.path.join(base, *p)) for p in parts]
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
# A take edit's undo record in media/.trash/: a file edit (apply_take_edit)
# or an item's change (record_annot_step, "annot", rows only).
EDIT_SIDECAR = re.compile(r"-before-(cut|uncut|pause|unpause|narrate|unnarrate|annot)-\d{8}-\d{6}\.json$")
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


def cut_rules(start, end):
    # What cutting start..end does to a take's overlays, narrations, pauses
    # and seams: (retime, narr, pz, cz), as apply_take_edit uses them. Shared
    # by the cut itself (cut_stretch) and its dry run (cut_drops), so the
    # warning names exactly what the cut would take.
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

    return retime, narr, pz, cz


def cut_drops(db, take_id, start, end):
    # What cutting start..end out of a take would remove, by cut_rules and
    # apply_take_edit's own tests: narrations and pauses it goes through,
    # items (callouts, shapes) left shorter than 0.05 s, and earlier cuts'
    # seams inside it. Nothing is changed.
    retime, narr, pz, cz = cut_rules(start, end)
    q = lambda sql: db.execute(sql, (take_id,)).fetchall()
    items = []
    for o in q("SELECT id, kind, text, shape, t_in, t_out FROM overlays WHERE take_id = ? ORDER BY t_in"):
        a, b = retime(o["t_in"], o["t_out"])
        if b - a < 0.05:
            items.append({"id": o["id"], "kind": o["shape"] or o["kind"], "text": o["text"],
                          "t_in": o["t_in"], "t_out": o["t_out"]})
    return {"narrations": [{"t_in": n["t_in"], "t_out": n["t_out"]}
                           for n in q("SELECT t_in, t_out FROM narrations WHERE take_id = ? ORDER BY t_in")
                           if narr(n["t_in"], n["t_out"]) is None],
            "pauses": [{"at": z["at"], "seconds": z["seconds"]}
                       for z in q("SELECT at, seconds FROM pauses WHERE take_id = ? ORDER BY at")
                       if pz(z["at"], z["seconds"]) is None],
            "items": items,
            "seams": [{"at": c["at"], "seconds": c["seconds"]}
                      for c in q("SELECT at, seconds FROM cuts WHERE take_id = ? ORDER BY at")
                      if cz(c["at"]) is None]}
TRASH = os.path.join(ROOT, ".trash")
EDIT_LOCK = threading.Lock()  # one undo, redo or history read at a time
# The takes with a video edit, an undo or a redo under way. One at a time per
# take: two at once would each read the same file, and the later swap would
# overwrite the earlier edit, leaving its retimed items and undo record
# behind. A second gets 409, which the page shows as a toast.
TAKES_EDITING = set()
TAKES_EDITING_LOCK = threading.Lock()


def claim_take(take_id):
    with TAKES_EDITING_LOCK:
        if take_id in TAKES_EDITING:
            return False
        TAKES_EDITING.add(take_id)
        return True


def release_take(take_id):
    with TAKES_EDITING_LOCK:
        TAKES_EDITING.discard(take_id)


BUSY_TAKE = "another edit of this scene is still running"


def take_editing(take_id):
    # Whether a video edit, undo or redo of the take is under way. Item writes
    # and starting a narration check it but don't claim the take: they are
    # quick, and two nudges in a row must not refuse each other. An item added
    # or changed mid-edit was made on the timeline before the edit, and would
    # be retimed or not by the edit depending on when it landed (trace
    # 2026-10-14: a pointer added during a pause insert was shifted by it).
    with TAKES_EDITING_LOCK:
        return take_id in TAKES_EDITING


# A take's edit history (the History tab): every edit leaves a sidecar in
# media/.trash/ (<stem>-before-<kind>-<when>.json, written by apply_take_edit)
# with the take's row, items and narrations as they were before it, and its
# files from before it beside it. The sidecars of one take file, in time
# order, are its stack. An undone edit keeps its sidecar, marked "undone",
# with the take as it was after the edit (files as <stem>-after-…, rows in
# the mark), so it can be redone. The edit in effect most recently is the
# cursor: the one Undo takes back. A new edit discards the undone ones.
# An item's change (a callout or shape added, changed or deleted) is a step
# too, of kind "annot": rows only, no files (record_annot_step), so it is
# undoable whenever it is the cursor, whatever the file.
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


def is_row_step(saved):
    # An item's change: rows only, nothing to match against the take's file.
    return saved.get("edit") == "annot"


def item_label(row):
    # "callout 'Scene 1'", "rectangle at 6.76 s": how History names an item.
    kind = {"rect": "rectangle", "ellipse": "oval"}.get(row.get("shape") or "", row.get("shape") or row.get("kind") or "item")
    return f"{kind} '{row['text']}'" if row.get("text") else f"{kind} at {row.get('t_in', 0):.2f} s"


def edit_what(name, saved):
    kind = edit_kind(name, saved)
    if kind == "annot":
        verb = {"add": "Added", "update": "Changed", "delete": "Deleted"}.get(saved.get("op"), "Changed")
        return f"{verb} {item_label(saved.get('after') or saved.get('before') or {})}"
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


# A take's audio, losslessly: media/.audio/<stem>.flac, beside a .json naming
# the MP4 audio it stands for (the MD5 of its AAC packets). Every edit builds
# the new audio from it and encodes AAC once, the same packets into the take
# and its clean copy. Re-encoding the last AAC on every edit wore the audio
# down: on one scene (2026-10-14) its first narration's energy above 8 kHz
# rose from 27 to 20 dB below the voice over ~25 edits. Only the current
# one is kept: when the MP4's audio is something else (Undo, Redo,
# remix_take.py, a file replaced by hand), audio_master rebuilds it from the
# MP4, one generation, rather than undo steps each keeping a copy.
AUDIO = os.path.join(ROOT, ".audio")
AAC = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]


def audio_md5(path):
    # The MD5 of a file's audio packets, as stream-copied: the same for the
    # take and its clean copy after an edit, and after overlay.py's render
    # (which copies the clean copy's audio).
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"],
                       capture_output=True, text=True)
    return r.stdout.strip().removeprefix("MD5=") if r.returncode == 0 else None


def master_paths(file):
    stem = file[:-4]
    return os.path.join(AUDIO, stem + ".flac"), os.path.join(AUDIO, stem + ".json")


def save_master(file, flac):
    # flac becomes the take's master, standing for its MP4's audio as it is.
    path, meta = master_paths(file)
    os.replace(flac, path)
    with open(meta, "w") as f:
        json.dump({"md5": audio_md5(os.path.join(ROOT, file))}, f)


def audio_master(file):
    # The take's master, rebuilt from its MP4 when missing or when it stands
    # for other audio than the MP4's. Raises if that fails.
    path, meta = master_paths(file)
    md5 = audio_md5(os.path.join(ROOT, file))
    try:
        with open(meta) as f:
            if json.load(f).get("md5") == md5 and md5 and os.path.isfile(path):
                return path
        why = "its MP4 changed"
    except (OSError, ValueError):
        why = "none yet"
    os.makedirs(AUDIO, exist_ok=True)
    tmp = os.path.join(AUDIO, f".{os.path.basename(path)}")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(ROOT, file), "-vn", "-map", "0:a:0",
                        "-c:a", "flac", "-ar", "48000", "-ac", "2", tmp], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or ["couldn't make the take's lossless audio"])[-1])
    save_master(file, tmp)
    print(f"audio: {file}'s lossless audio made from its MP4 ({why})", flush=True)
    return path


def seam_cut(path, start, end, out, audio):
    # Cut start..end out of a take's file (a cut, or removing a pause:
    # cut_stretch): seam_splice of what's left on either side. Frames are
    # kept as trim keeps them: t < start, t >= end. A full re-encode of each
    # file was ~3.5-5 s; this was 1.1-3.0 s on the same cuts, with the same
    # frame count, durations within a frame, a clean decode, and every frame
    # 43-53 dB PSNR from the full re-encode (prototype, 2026-10-13).
    return seam_splice([("span", path, 0.0, start), ("span", path, end, None)], out, audio)


def seam_splice(pieces, out, audio):
    # Join pieces into one take file, re-encoding the video only around the
    # seams. A piece is ("span", file, t0, t1): that file's frames with
    # t0 <= t < t1 (t1 None: to its end); or ("hold", file, t, secs): the
    # frame at t (its last frame before t; the first frame for t < 0.02)
    # held for secs. Within a span, whole keyframe groups are stream-copied
    # by exact frame count, and only the frames from the span's start to its
    # first keyframe, and from its last keyframe to its end, are re-encoded
    # (overlay.ENC, the takes' settings); a hold is encoded as its own short
    # piece. The pieces are joined by stream copy, with the edit's audio:
    # audio() gives its file, made from the take's lossless audio
    # (_apply_take_edit) while the pieces are encoded. All files are the
    # take's own (the scene and its before-edit copies), so their codec
    # settings match. Returns the number of frames re-encoded; raises on
    # anything unexpected, and the caller falls back to the full chain.
    info = {}

    def file_info(path):
        if path not in info:
            rate = probe(path, "stream=r_frame_rate", "v:0")
            num, den = (int(x) for x in rate.split("/"))
            packets = [l.split(",") for l in probe(path, "packet=pts_time,flags", "v:0").splitlines() if l]
            fps = num / den
            keys = sorted(round(float(p[0]) * fps) for p in packets if len(p) > 1 and "K" in p[1])
            if not keys or keys[0] != 0:
                raise ValueError(f"{os.path.basename(path)}: the first frame isn't a keyframe")
            info[path] = {"rate": rate, "fps": fps, "total": len(packets), "keys": keys,
                          "dur": float(probe(path, "format=duration") or 0)}
        return info[path]

    rates = {file_info(p[1])["rate"] for p in pieces}
    if len(rates) != 1:
        raise ValueError(f"frame rates differ: {sorted(rates)}")
    rate = rates.pop()
    frame = lambda f, t: math.ceil(t * f["fps"] - 1e-6)
    work = tempfile.mkdtemp(prefix="seam-")
    try:
        enc = [*overlay.ENC, "-r", rate]
        names, jobs, reencoded = [], [], 0

        def piece(cmd):
            name = f"p{len(names):02d}.mp4"
            names.append(name)
            jobs.append(["ffmpeg", "-v", "error", "-y", *cmd, os.path.join(work, name)])

        for kind, path, t, x in pieces:
            f = file_info(path)
            fps = f["fps"]
            if kind == "hold":
                n = round(x * fps)
                held = 0 if t < 0.02 else min(frame(f, t), f["total"]) - 1
                if n < 1 or held < 0:
                    raise ValueError(f"hold of {n} frames at frame {held}")
                piece(["-ss", f"{held / fps:.6f}", "-i", path, "-an",
                       "-vf", f"trim=end_frame=1,tpad=stop_mode=clone:stop={n - 1}", "-frames:v", str(n), *enc])
                reencoded += n
                continue
            fs = frame(f, t)
            fe = f["total"] if x is None else min(frame(f, x), f["total"])
            if not 0 <= fs < fe:
                continue  # an empty span (a pause at the very start or end)
            bounds = [k for k in f["keys"] if fs <= k <= fe] + ([fe] if fe == f["total"] else [])
            c0, c1 = (min(bounds), max(bounds)) if bounds else (fe, fe)
            if c1 <= c0:  # no whole keyframe group inside: re-encode the span
                c0 = c1 = fe
            if c0 > fs:
                piece(["-ss", f"{fs / fps:.6f}", "-i", path, "-an", "-frames:v", str(c0 - fs), *enc])
                reencoded += c0 - fs
            if c1 > c0:
                piece(["-ss", f"{c0 / fps:.6f}", "-i", path, "-an", "-c:v", "copy", "-frames:v", str(c1 - c0)])
            if fe > c1:
                piece(["-ss", f"{c1 / fps:.6f}", "-i", path, "-an", "-frames:v", str(fe - c1), *enc])
                reencoded += fe - c1

        with ThreadPoolExecutor(max(1, len(jobs))) as ex:
            results = list(ex.map(lambda c: subprocess.run(c, capture_output=True, text=True), jobs))
        for r in results:
            if r.returncode:
                raise RuntimeError((r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        listing = os.path.join(work, "list.txt")
        with open(listing, "w") as f:
            f.writelines(f"file '{os.path.join(work, n)}'\n" for n in names)
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", listing, "-i", audio(),
                            "-map", "0:v", "-map", "1:a", "-c", "copy", out], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError((r.stderr.strip().splitlines() or ["joining failed"])[-1])
        return reencoded
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    if cursor is not None and not is_row_step(edits[cursor][1]) and edits[cursor][1].get("after") != live:
        cursor, why = None, "This take's file has changed since its last edit, so that edit can't be undone."
    if redo is not None and not is_row_step(edits[redo][1]) and edits[redo][1]["undone"].get("stat") != live:
        redo = None
    return edits, cursor, redo, why


def trace_on():
    # Whether XMLUI's trace capture is on (config.json's xmluiConfig.xsVerbose;
    # appGlobals.xsVerbose, its deprecated place, still counts if present).
    try:
        with open(CONFIG) as f:
            config = json.load(f)
        on = config.get("xmluiConfig", {}).get("xsVerbose")
        return (on if on is not None else config.get("appGlobals", {}).get("xsVerbose")) is True
    except (OSError, ValueError):
        return False


def discard_undone(file):
    # A new edit ends the chance to redo: the undone edits above the cursor
    # go, with the files kept for redoing them. The caller holds EDIT_LOCK.
    for name, old in take_edits(file):
        if old.get("undone"):
            for f in list((old["undone"] or {}).get("files", {}).values()) + [name]:
                if os.path.exists(os.path.join(TRASH, f)):
                    os.remove(os.path.join(TRASH, f))


def record_annot_step(take_file, op, item_id, before, after):
    # An item's change as a step of the take's history: its row before and
    # after (None: it didn't exist). Consecutive changes to one item are one
    # step: a change to the item the newest step changed extends that step,
    # however long after, until something else is recorded (another item, a
    # cut, a narration...) or the step is sealed by Undo or Redo (lastAt 0,
    # seal_cursor). A 3 s window, the first rule, split ordinary editing into
    # a line per save (2026-10-05).
    os.makedirs(TRASH, exist_ok=True)
    now = time.time()
    with EDIT_LOCK:
        discard_undone(take_file)
        edits = take_edits(take_file)
        if edits and op == "update":
            name, last = edits[-1]
            if (is_row_step(last) and last.get("id") == item_id and last.get("op") in ("add", "update")
                    and last.get("lastAt")):
                last["after"], last["lastAt"] = after, now
                with open(os.path.join(TRASH, name), "w") as f:
                    json.dump(last, f, indent=1)
                return
        stem, t = take_file[:-4], int(now)
        while True:  # one name per second: two changes in one second get the next free one
            name = f"{stem}-before-annot-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}.json"
            if not os.path.exists(os.path.join(TRASH, name)):
                break
            t += 1
        with open(os.path.join(TRASH, name), "w") as f:
            json.dump({"edit": "annot", "op": op, "id": item_id, "before": before, "after": after,
                       "lastAt": now}, f, indent=1)


def seal_cursor(take_file):
    # After an Undo, the step now in effect is sealed (lastAt 0), so the next
    # change to its item starts a new step rather than extending one that
    # Undo has already stepped back past. The caller holds EDIT_LOCK.
    done = [e for e in take_edits(take_file) if not e[1].get("undone")]
    if done and is_row_step(done[-1][1]) and done[-1][1].get("lastAt"):
        name, saved = done[-1]
        saved["lastAt"] = 0
        with open(os.path.join(TRASH, name), "w") as f:
            json.dump(saved, f, indent=1)


def set_item_row(db, take_id, item_id, row):
    # An item's row as a step has it: gone when None.
    db.execute("DELETE FROM overlays WHERE id = ? AND take_id = ?", (item_id, take_id))
    if row:
        r = {**row, "id": item_id, "take_id": take_id}
        db.execute(f"INSERT INTO overlays ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))


def resync_history(file, was):
    # The take's file was rewritten without being an edit (Export burns the
    # items in): where the history described the file as it was (`was`, its
    # size and mtime before), point it at the file as it is now. Item steps
    # describe no file, so the newest file step is the one to check.
    with EDIT_LOCK:
        edits = [e for e in take_edits(file) if not is_row_step(e[1])]
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
    # Time to the microsecond: a copy (a project moved into projects/,
    # Safekeeping) keeps only that much, and an exact nanosecond match
    # disowned Studio's own exports (export-record-mtime-precision).
    if e and e.get("size") == st.st_size and isinstance(e.get("mtime_ns"), int) \
            and e["mtime_ns"] // 1000 == st.st_mtime_ns // 1000:
        return e.get("kind")
    return None


# A free-text note per listed file, saying what it is ("edit of the 22:09
# export, intro cut"), since names can't: {key: note}, key = the name in
# media/exports/.
NOTES = os.path.join(ROOT, "exports", ".notes.json")
NOTES_LOCK = threading.Lock()


# The mic narrations record from, and the whisper model register.py uses.
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
# a dictation does. Recorded with the native recorder (record_native.swift),
# not ffmpeg's capture, which drops ~10% of samples (see voicetest.py).
narration = {"current": None}
NATIVE_SRC = os.path.join(HERE, "record_native.swift")
NATIVE_BIN = os.path.expanduser("~/.cache/bram-studio/record_native")
# A narration's voice chain: the denoise, then highpass and compression.
NARRATE_AF = ("anlmdn=s=0.0005:p=0.002:r=0.006,highpass=f=80,"
              "acompressor=threshold=-24dB:ratio=3:attack=5:release=120")
NARRATE_FLOOR = -45.0  # LUFS: a recording quieter than this has no speech in it
NARRATE_GRACE = 30  # s past the clip's length before an unstopped narration is abandoned


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


# Transcripts and speakers (the Speakers page). A source is transcribed by
# whisper-cli into work/transcripts/<source stem>/, and its segments (source
# seconds) are loaded into the project's database, where each can be given a
# speaker. Speakers belong to the project, not to a source. The page reads all
# three tables through /query; these routes are the only writers, so the
# agent labels through them too and every write announces itself on /events
# ({"changed": "transcripts"|"speakers"}). The recipe is the one first run by
# hand for the AIW recording: 16 kHz mono WAV, the large-v3-turbo model with a
# prompt naming who is in it.
#
# The prompt starts with disfluent text (DISFLUENT_PROMPT), which makes whisper
# write fillers and repeats as spoken instead of tidying them away, so they can
# be cut; and it is given once, not repeated for every 30 s window
# (--carry-initial-prompt). On the AIW recording, repeated names-only prompting
# gave 24 fillers, 40 repeats and 82 lowercase, unpunctuated segments; this gave
# 52, 68 and 29 (probe, 2026-11-13). whisper's DTW word alignment was tried and
# not used: against 75 pause ends its word starts were 0.44 s late, the default
# token times -0.03 s.
DISFLUENT_PROMPT = "Um, so, uh, I was like, you know, I mean, hmm. Uh,"
TRANSCRIPTS_SQL = """CREATE TABLE IF NOT EXISTS transcripts (
  source TEXT PRIMARY KEY,
  model TEXT,
  prompt TEXT,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  duration_s REAL
)"""
SEGMENTS_SQL = """CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  t0 REAL NOT NULL,
  t1 REAL NOT NULL,
  text TEXT NOT NULL,
  speaker_id INTEGER
)"""
SPEAKERS_SQL = """CREATE TABLE IF NOT EXISTS speakers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT NOT NULL,
  position INTEGER
)"""
WHISPER_MODELS = os.path.expanduser("~/.local/share/whisper-models")
# Largest first; WHISPER_MODEL, when set, is used as given.
TRANSCRIBE_MODELS = ("ggml-large-v3-turbo.bin", "ggml-large-v3.bin", "ggml-small.en.bin")
SPEAKER_COLORS = ("#2a78d6", "#d6602a", "#2a9d5c", "#9b4dca", "#c9a227", "#d63a6b", "#1aa3a3", "#7a7a7a")
SEGMENT_LINE = re.compile(r"^\[(\d+):(\d+):([\d.]+) --> ")
transcribing = {}  # source -> {"done_s", "duration_s"} while whisper-cli runs; one at a time
TRANSCRIBE_LOCK = threading.Lock()
TEXT_LOCK = threading.Lock()  # one text edit (save, undo, restore) at a time
ALIGN_LOCK = threading.Lock()  # one align.py run at a time (aligned_edges)


def align_soon(source):
    # Place a change's new cut edges in the background, so the next paragraph
    # played needn't wait for them (Elide tics' 144 new edges took 10.6 s).
    def run():
        try:
            db = sqlite3.connect(DB)
            row = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()
            if row:
                text_cuts(db, source, row[0] or 0)
        except Exception as e:
            print(f"align soon failed source={source} error={e}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def transcribe_model():
    if os.environ.get("WHISPER_MODEL"):
        return os.environ["WHISPER_MODEL"]
    for name in TRANSCRIBE_MODELS:
        path = os.path.join(WHISPER_MODELS, name)
        if os.path.isfile(path):
            return path
    return None


def run_transcript(source, path, prompt, model):
    # The background half of POST /transcripts/start: audio out, whisper-cli
    # over it (its segment lines are the progress), segments in. Progress goes
    # to the page as {"transcribing": source, done_s, duration_s} every 2 s.
    t_start = time.time()
    folder = os.path.join(WORK, "transcripts", os.path.splitext(source)[0])
    os.makedirs(folder, exist_ok=True)
    wav, out = os.path.join(folder, "audio-16k.wav"), os.path.join(folder, "transcript")
    state = transcribing[source]

    def finish(status):
        with sqlite3.connect(DB) as db:
            db.execute("UPDATE transcripts SET status = ?, finished_at = ? WHERE source = ?",
                       (status, datetime_now(), source))
        with TRANSCRIBE_LOCK:
            transcribing.pop(source, None)
        notify("transcripts")

    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-vn", "-ac", "1",
                            "-ar", "16000", "-c:a", "pcm_s16le", wav], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("audio: " + (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        args = ["whisper-cli", "-m", model, "-f", wav, "-l", "en", "-t", "8", "-ojf", "-osrt", "-otxt", "-of", out,
                "--prompt", f"{DISFLUENT_PROMPT} {prompt}".strip()]
        with open(os.path.join(folder, "whisper.log"), "w") as log:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=log, text=True)
            last_push = 0
            for line in proc.stdout:
                log.write(line)
                m = SEGMENT_LINE.match(line)
                if m:
                    state["done_s"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    if time.time() - last_push > 2:
                        last_push = time.time()
                        push({"transcribing": source, **state})
            if proc.wait():
                raise RuntimeError(f"whisper-cli exited {proc.returncode} (see {folder}/whisper.log)")
        rows = [(source, s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s["text"].strip())
                for s in json.load(open(out + ".json"))["transcription"] if s["text"].strip()]
        with sqlite3.connect(DB) as db:
            db.execute("DELETE FROM segments WHERE source = ?", (source,))
            db.executemany("INSERT INTO segments (source, t0, t1, text) VALUES (?, ?, ?, ?)", rows)
            n_words = build_words(db, source)
        print(f"transcribe done source={source} segments={len(rows)} words={n_words} "
              f"secs={time.time() - t_start:.1f}", flush=True)
        finish("done")
    except Exception as e:
        print(f"transcribe failed source={source} error={e}", flush=True)
        finish(f"failed: {e}")


def datetime_now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# Editing a podcast by editing its text (the Podcast page's Edit text tab).
# A transcript's words, built from whisper's tokens (transcript.json), carry
# their source times; editing the text marks words cut, corrects how they
# read, or restores them, and never moves a time. The cut list (text_cuts)
# turns the cut words into source spans whose edges sit in the quiet between
# words, which is the edit list a later step renders.
WORDS_SQL = """CREATE TABLE IF NOT EXISTS words (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  i INTEGER NOT NULL,
  t0 REAL NOT NULL,
  t1 REAL NOT NULL,
  text TEXT NOT NULL,
  shown TEXT,
  kept INTEGER NOT NULL DEFAULT 1,
  cut_reason TEXT,
  speaker_id INTEGER,
  para INTEGER,
  speaker_how TEXT,
  speaker_unsure INTEGER
)"""
# Each save, restore or tic elision is a row; Undo marks the latest kept row
# undone (it stays, for Redo), Redo re-applies the earliest undone one, and a
# new change discards the undone rows, as a scene's Undo and Redo do.
TRANSCRIPT_EDITS_SQL = """CREATE TABLE IF NOT EXISTS transcript_edits (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  changes TEXT NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0
)"""
# Paragraphs of the editable text (words.para): a new one after a sentence
# that ends in a pause over the paragraphPauseS setting, or after any
# sentence once a paragraph passes paragraphWords (PODCAST_SETTINGS). On AIW, pauses alone gave 11 paragraphs at
# 1.5 s and still one of 845 words at 0.5 s. Computed once, from all the
# words (assign_paras), so cutting words never moves a break: the Edit text
# tab edits and plays a paragraph at a time.
SNAP_WINDOW = 0.25  # a cut edge moves at most this far to reach quiet
# Cut edges by the sound (align.py): a CTC speech model under sherpa-onnx, in
# its own venv, since Homebrew's Python refuses system-wide installs. Both are
# optional: without them, cut edges fall back to snap() and are flagged.
SPEECH_PYTHON = os.path.expanduser("~/.local/share/bram-studio/venv/bin/python")
CTC_MODEL = os.environ.get("CTC_MODEL", os.path.expanduser(
    "~/.local/share/speech-models/sherpa-onnx-nemo-ctc-en-conformer-small"))
WORD_EDGES_SQL = """CREATE TABLE IF NOT EXISTS word_edges (
  source TEXT NOT NULL,
  i INTEGER NOT NULL,
  t REAL NOT NULL,
  method TEXT NOT NULL,
  PRIMARY KEY (source, i)
)"""
# Notes on the final transcript's words (docs/annotation.md): the words they
# cover as word ids from..to, the words' text when saved (to spot a stale
# note), and the note's Markdown. Called "notes" because "annotations"
# already names the video callouts' routes.
NOTES_SQL = """CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  w_from INTEGER NOT NULL,
  w_to INTEGER NOT NULL,
  words TEXT NOT NULL,
  markdown TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  provisional INTEGER NOT NULL DEFAULT 0
)"""
checking = {}  # source -> {"started"} while a round-trip check runs (check_text)
matching = {}  # source -> {"done", "total"} while voiceprint.py runs (match_speakers)
SPEAKER_MODEL = os.environ.get("SPEAKER_MODEL", os.path.expanduser(
    "~/.local/share/speech-models/nemo_en_titanet_small.onnx"))
PROGRESS_LINE = re.compile(r"progress (\d+) (\d+)")


sample_checks = {}  # source -> {"rerun"} while voiceprint.py --check runs (check_samples)
SAMPLE_LOCK = threading.Lock()


def samples_key(db, source):
    # What the samples are now: a hash of every tagged word and its speaker.
    # The last check is current while it matches.
    h = hashlib.sha1()
    for wid, spk in db.execute("SELECT id, speaker_id FROM words WHERE source = ? AND speaker_how = 'tag' AND kept = 1 "
                               "ORDER BY i", (source,)):
        h.update(f"{wid}:{spk};".encode())
    return h.hexdigest()[:12]


def check_samples(source):
    # In the background: voiceprint.py --check, which flags runs of windows
    # in a sample stretch that sound like another speaker; the result is
    # kept in the transcript's folder (samples-check.json) with the samples
    # key it was made for, and /events gets {"samplesChecked": source}.
    # Started when the sample list is asked for and the last check isn't
    # current; asked again while running, it runs once more after.
    while True:
        t_start = time.time()
        try:
            db = sqlite3.connect(DB)
            key = samples_key(db, source)
            with SAMPLE_LOCK:
                sample_checks[source]["key"] = key
            conf = podcast_settings()
            env = {**os.environ, "VP_WINDOW": str(conf["vpWindowS"]), "VP_STEP": str(conf["vpStepS"]),
                   "VP_MIN_TURN": str(conf["vpMinTurnS"]), "VP_MIN_TAGGED": str(conf["vpMinTaggedS"]),
               "VP_EDGE": str(conf["vpEdgeS"]),
                   "SPEAKER_MODEL": SPEAKER_MODEL}
            r = subprocess.run([SPEECH_PYTHON, os.path.join(HERE, "voiceprint.py"),
                                os.path.join(transcript_folder(source), "audio-16k.wav"), DB, source, "--check"],
                               capture_output=True, text=True, env=env)
            if r.returncode:
                raise RuntimeError((r.stderr.strip().splitlines() or ["voiceprint.py --check failed"])[-1])
            result = json.loads(r.stdout.strip().splitlines()[-1])
            result["key"] = key
            path = os.path.join(transcript_folder(source), "samples-check.json")
            with open(path + ".tmp", "w") as f:
                json.dump(result, f)
            os.replace(path + ".tmp", path)
            flags = sum(len(x["flags"]) for x in result.get("stretches", []))
            print(f"samples check source={source} stretches={len(result.get('stretches', []))} flags={flags} "
                  f"secs={time.time() - t_start:.1f}", flush=True)
        except Exception as e:
            print(f"samples check failed source={source} error={e}", flush=True)
        with SAMPLE_LOCK:
            if not sample_checks.get(source, {}).get("rerun"):
                sample_checks.pop(source, None)
                break
            sample_checks[source] = {"rerun": False, "key": None}
    push({"samplesChecked": source})


def sample_list(db, source):
    # GET /speakers/samples: every sample stretch (consecutive tagged words,
    # one speaker), in order, with the last check's flags when it is
    # current; and whether a check is running or due (which starts it).
    names = {i: (n, c) for i, n, c in db.execute("SELECT id, name, color FROM speakers")}
    # Kept words only, numbered along the kept words (k), so a cut word is in
    # no sample and doesn't split one; voiceprint.py builds its stretches the
    # same way (sample-clear-cut-words).
    rows = db.execute("SELECT id, k, t0, t1, w, speaker_id FROM ("
                      "SELECT id, ROW_NUMBER() OVER (ORDER BY i) AS k, t0, t1, coalesce(shown, text) AS w, "
                      "speaker_id, speaker_how FROM words WHERE source = ? AND kept = 1) "
                      "WHERE speaker_how = 'tag' ORDER BY k", (source,)).fetchall()
    out, run = [], None
    for wid, i, t0, t1, w, spk in rows:
        if run and run["speaker"] == spk and run["_i"] == i - 1:
            run["_i"], run["t1"], run["toId"] = i, t1, wid
            run["_w"].append((t0, w, wid))
        else:
            name, color = names.get(spk, ("a deleted speaker", None))
            run = {"speaker": spk, "name": name, "color": color, "t0": t0, "t1": t1, "fromId": wid,
                   "toId": wid, "_i": i, "_w": [(t0, w, wid)]}
            out.append(run)
    key = samples_key(db, source)
    try:
        last = json.load(open(os.path.join(transcript_folder(source), "samples-check.json")))
    except (OSError, ValueError):
        last = {}
    current = last.get("key") == key
    # The last check's flags, by stretch (first word, speaker, end). While a
    # new check runs, a stretch that hasn't changed keeps its flags rather
    # than all of them vanishing for its ~9 s.
    flags = {(x["firstWord"], x["speaker"], round(x["t1"], 1)): x["flags"] for x in last.get("stretches", [])}
    for r in out:
        ws = r.pop("_w")
        r.pop("_i")
        r["secs"] = round(r["t1"] - r["t0"], 1)
        r["text"] = " ".join(w for _, w, _ in ws[:10]) + ("…" if len(ws) > 10 else "")
        r["flags"] = []
        for f in flags.get((r["fromId"], r["speaker"], round(r["t1"], 1)), []):
            inside = [(w, wid) for t, w, wid in ws if f["t0"] <= t < f["t1"]]
            # Its words' ids, for Clear on just this part.
            r["flags"].append({**f, "likeName": names.get(f["like"], ("someone else",))[0],
                               "text": " ".join(w for w, _ in inside),
                               "fromId": inside[0][1] if inside else None,
                               "toId": inside[-1][1] if inside else None})
        r["key"] = f"{r['fromId']}-{r['toId']}-{r['speaker']}"
    due = not current and bool(out) and os.path.isfile(SPEECH_PYTHON) and os.path.isfile(SPEAKER_MODEL)
    with SAMPLE_LOCK:
        if due:
            if source in sample_checks:
                if sample_checks[source].get("key") != key:
                    sample_checks[source]["rerun"] = True
            else:
                sample_checks[source] = {"rerun": False, "key": key}
                threading.Thread(target=check_samples, args=(source,), daemon=True).start()
        checking = source in sample_checks
    return {"stretches": out, "checked": current, "checking": checking}


def match_speakers(source):
    # The background half of POST /speakers/propagate: voiceprint.py over
    # the source, then its speakers written to the words you didn't tag
    # (speaker_how 'voiceprint'), replacing the last run's.
    t_start = time.time()
    try:
        conf = podcast_settings()
        env = {**os.environ, "VP_WINDOW": str(conf["vpWindowS"]), "VP_STEP": str(conf["vpStepS"]),
               "VP_MIN_TURN": str(conf["vpMinTurnS"]), "VP_MIN_TAGGED": str(conf["vpMinTaggedS"]),
               "VP_EDGE": str(conf["vpEdgeS"]),
               "SPEAKER_MODEL": SPEAKER_MODEL}
        proc = subprocess.Popen([SPEECH_PYTHON, os.path.join(HERE, "voiceprint.py"),
                                 os.path.join(transcript_folder(source), "audio-16k.wav"), DB, source],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        out_lines = []
        reader = threading.Thread(target=lambda: out_lines.extend(proc.stdout), daemon=True)
        reader.start()
        for line in proc.stderr:
            m = PROGRESS_LINE.match(line)
            if m:
                state = {"done": int(m.group(1)), "total": int(m.group(2))}
                matching[source] = state
                push({"matching": source, **state})
        reader.join()
        if proc.wait():
            raise RuntimeError(f"voiceprint.py exited {proc.returncode}")
        result = json.loads(out_lines[-1])
        if result.get("error"):
            raise RuntimeError(result["error"])
        db = sqlite3.connect(DB)
        db.execute("UPDATE words SET speaker_id = NULL, speaker_how = NULL, speaker_unsure = NULL "
                   "WHERE source = ? AND speaker_how = 'voiceprint'", (source,))
        db.executemany("UPDATE words SET speaker_id = ?, speaker_how = 'voiceprint', speaker_unsure = ? "
                       "WHERE id = ? AND (speaker_how IS NULL OR speaker_how = 'voiceprint')",
                       [(spk, unsure, wid) for wid, spk, unsure in result["assign"]])
        db.commit()
        unsure = sum(1 for a in result["assign"] if a[2])
        edges = result.get("edges") or {}
        print(f"match source={source} speakers={json.dumps(result['speakers'])} windows={result['windows']} "
              f"edges moved={edges.get('moved', 0)} of {edges.get('total', 0)} "
              f"words={len(result['assign'])} unsure={unsure} secs={time.time() - t_start:.1f}", flush=True)
    except Exception as e:
        print(f"match failed source={source} error={e}", flush=True)
        push({"matching": source, "error": str(e)})
    finally:
        with TRANSCRIBE_LOCK:
            matching.pop(source, None)
        push({"matching": source, "finished": True})
        notify("words")
WORD_KEY = re.compile(r"[^\w']+")


def transcript_folder(source):
    return os.path.join(WORK, "transcripts", os.path.splitext(source)[0])


def build_words(db, source):
    # whisper's tokens joined into words: a token starting with a space
    # starts a word, any other (a word's tail, punctuation) joins the one
    # before; [_BEG_]-style markers are skipped. Replaces the source's words
    # and its edit history.
    path = os.path.join(transcript_folder(source), "transcript.json")
    words = []
    for seg in json.load(open(path))["transcription"]:
        for tok in seg.get("tokens", []):
            text = tok["text"]
            if text.startswith("[_") or not text.strip():
                continue
            t0, t1 = tok["offsets"]["from"] / 1000, tok["offsets"]["to"] / 1000
            if words and not text.startswith(" "):
                words[-1][1] = t1
                words[-1][2] += text
            else:
                words.append([t0, t1, text.strip()])
    db.execute("DELETE FROM words WHERE source = ?", (source,))
    db.execute("DELETE FROM transcript_edits WHERE source = ?", (source,))
    db.execute("DELETE FROM word_edges WHERE source = ?", (source,))
    try:
        os.remove(os.path.join(transcript_folder(source), "check.json"))
    except OSError:
        pass
    db.executemany("INSERT INTO words (source, i, t0, t1, text) VALUES (?, ?, ?, ?, ?)",
                   [(source, i, w[0], max(w[0], w[1]), w[2]) for i, w in enumerate(words)])
    assign_paras(db, source)
    return len(words)


def assign_paras(db, source):
    # words.para for every word of the source, kept or cut, by the
    # paragraph settings, which are noted beside the transcript so a project
    # opened after they change is re-split (ensure_words).
    conf = podcast_settings()
    gap, most = conf["paragraphPauseS"], conf["paragraphWords"]
    para, prev, run, rows = 0, None, 0, []
    for wid, t0, t1, text in db.execute("SELECT id, t0, t1, text FROM words WHERE source = ? ORDER BY i",
                                        (source,)).fetchall():
        if prev is not None and prev[1][-1:] in ".?!" and (t0 - prev[0] > gap or run >= most):
            para, run = para + 1, 0
        rows.append((para, wid))
        prev, run = (t1, text), run + 1
    db.executemany("UPDATE words SET para = ? WHERE id = ?", rows)
    try:
        json.dump({"gap": gap, "words": most}, open(os.path.join(transcript_folder(source), "paras.json"), "w"))
    except OSError:
        pass


def ensure_words(db, source):
    # Words for a transcript made before the words table existed, and
    # paragraphs for words made before words.para did.
    if db.execute("SELECT 1 FROM words WHERE source = ? LIMIT 1", (source,)).fetchone():
        conf = podcast_settings()
        try:
            stamp = json.load(open(os.path.join(transcript_folder(source), "paras.json")))
        except (OSError, ValueError):
            stamp = {}
        if (db.execute("SELECT 1 FROM words WHERE source = ? AND para IS NULL LIMIT 1", (source,)).fetchone()
                or stamp != {"gap": conf["paragraphPauseS"], "words": conf["paragraphWords"]}):
            assign_paras(db, source)
            db.commit()
            print(f"paragraphs assigned source={source}", flush=True)
        return True
    if not os.path.isfile(os.path.join(transcript_folder(source), "transcript.json")):
        return False
    n = build_words(db, source)
    db.commit()
    print(f"words built source={source} words={n}", flush=True)
    return True


def word_key(text):
    return WORD_KEY.sub("", text.lower())


def kept_text(db, source):
    # The kept words as running text, a blank line between paragraphs.
    out, prev = [], None
    for para, text in db.execute(
            "SELECT para, coalesce(shown, text) FROM words WHERE source = ? AND kept = 1 ORDER BY i", (source,)):
        if prev is not None:
            out.append("\n\n" if para != prev else " ")
        out.append(text)
        prev = para
    return "".join(out)


def paragraphs(db, source, with_words=False):
    # Each paragraph's kept text, its counts and a version: a hash of its
    # words' state, which changes whenever an edit touches it (the page keys
    # rows and audio by it, so a changed paragraph redraws and re-renders).
    paras = {}
    # Tics still kept, per paragraph (what Elide tics would cut there).
    para_of = {}
    for wid, para in db.execute("SELECT id, para FROM words WHERE source = ?", (source,)):
        para_of[wid] = para
    ticky = collections.Counter(para_of[i] for i in elide_tics(db, source, dry=True)["ids"])
    colors = dict(db.execute("SELECT id, color FROM speakers"))
    # Each paragraph carries the speakers in tag mode, for its own "Who said
    # it?" buttons: the page's data can't be read reliably from inside the
    # list (xmlui-org/xmlui#3940).
    roster = [{"id": i, "name": n, "color": c}
              for i, n, c in db.execute("SELECT id, name, color FROM speakers ORDER BY position, id")]
    for para, wid, kept, shown, text, reason, spk, how, unsure in db.execute(
            "SELECT para, id, kept, shown, text, cut_reason, speaker_id, speaker_how, speaker_unsure "
            "FROM words WHERE source = ? ORDER BY i", (source,)):
        p = paras.setdefault(para, {"para": para, "words": [], "wl": [], "kept": 0, "cut": 0, "tics": 0,
                                    "h": hashlib.sha1()})
        if kept:
            p["words"].append(shown or text)
            p["wl"].append({"id": wid, "w": shown or text, "s": spk, "h": how, "u": unsure or 0,
                            "c": colors.get(spk)})
            p["kept"] += 1
        else:
            p["cut"] += 1
            p["tics"] += reason == "tic"
        p["h"].update(f"{wid}:{kept}:{shown or ''}:{spk}:{how}:{unsure};".encode())
    # av: the paragraph's sound version (audio_versions), what its clip is
    # named by. The page addresses the clip by it, not by v, so tagging or
    # matching voices doesn't reload the Workspace's video
    # (workspace-video-steady).
    sound = audio_versions(db, source)
    return [{"para": p["para"], "text": " ".join(p["words"]), "kept": p["kept"], "cut": p["cut"],
             "ticsCut": p["tics"], "ticsKept": ticky.get(p["para"], 0),
             **({"wl": p["wl"], "speakers": roster} if with_words else {}),
             "v": p["h"].hexdigest()[:10], "av": sound.get(p["para"]),
             "key": f"{p['para']}-{p['h'].hexdigest()[:10]}-{ticky.get(p['para'], 0)}"} for p in paras.values()]


def describe_step(db, source, ch):
    # A change in words, for the Undo / Redo line: what it did and where,
    # the paragraph named by its opening words (the page doesn't number
    # them). Returns (text, the first paragraph it touched).
    cut, back, fixed = ch.get("cut", []), ch.get("restored", []), ch.get("corrected", [])
    tagged = ch.get("tagged", [])
    ids = cut + back + [c[0] for c in fixed] + [t["id"] for t in tagged]
    if not ids:
        return "", None
    paras = sorted({r[0] for r in db.execute(
        f"SELECT para FROM words WHERE id IN ({','.join('?' * len(ids))})", ids)})
    first = paras[0]
    # The paragraph's opening as its box shows it (kept words, corrected);
    # a paragraph that's all cut, by its original words.
    opening = " ".join(r[0] for r in db.execute(
        "SELECT coalesce(shown, text) FROM words WHERE source = ? AND para = ? AND kept = 1 ORDER BY i LIMIT 5",
        (source, first))) or " ".join(r[0] for r in db.execute(
        "SELECT text FROM words WHERE source = ? AND para = ? ORDER BY i LIMIT 5", (source, first)))
    more = len(paras) - 1
    where = f"“{opening}…”" + (f" and {more} more paragraph{'s' if more > 1 else ''}" if more else "")
    n = lambda k, one, many: f"{k} {one if k == 1 else many}"
    if ch.get("reason") == "tag":
        spk = tagged[0]["now"][0] if tagged else None
        name = (db.execute("SELECT name FROM speakers WHERE id = ?", (spk,)).fetchone() or ["a deleted speaker"])[0] \
            if spk is not None else None
        what = (f"tag {n(len(tagged), 'word', 'words')} as {name}" if name
                else f"clear the speaker from {n(len(tagged), 'word', 'words')}")
    elif ch.get("reason") == "tic":
        what = "elide " + n(len(cut), "tic", "tics")
    elif ch.get("reason") == "replace":
        what = f"replace {ch.get('before')} with {ch.get('after')} ({n(len(fixed), 'word', 'words')})"
    elif back and not cut and not fixed:
        was = ch.get("restoredWas", {})
        what = "restore " + (n(len(back), "tic", "tics") if was and all(v == "tic" for v in was.values())
                             else n(len(back), "word", "words"))
    else:
        what = "your edit (" + ", ".join(x for x in (n(len(cut), "word cut", "words cut") if cut else "",
                                                     n(len(fixed), "correction", "corrections") if fixed else "",
                                                     n(len(back), "word restored", "words restored") if back else "")
                                         if x) + ")"
    return f"{what} in {where}", first


def text_summary(db, source):
    kept, kept_s, cut, cut_s = db.execute(
        "SELECT sum(kept), sum(CASE WHEN kept THEN t1 - t0 ELSE 0 END), sum(1 - kept), "
        "sum(CASE WHEN kept THEN 0 ELSE t1 - t0 END) FROM words WHERE source = ?", (source,)).fetchone()
    done, undone = db.execute("SELECT coalesce(sum(1 - undone), 0), coalesce(sum(undone), 0) "
                              "FROM transcript_edits WHERE source = ?", (source,)).fetchone()
    nxt_undo = db.execute("SELECT changes FROM transcript_edits WHERE source = ? AND undone = 0 "
                          "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
    nxt_redo = db.execute("SELECT changes FROM transcript_edits WHERE source = ? AND undone = 1 "
                          "ORDER BY id LIMIT 1", (source,)).fetchone()
    return {"text": kept_text(db, source), "keptWords": kept or 0, "cutWords": cut or 0,
            "canUndo": done > 0, "canRedo": undone > 0,
            "undoLabel": describe_step(db, source, json.loads(nxt_undo[0]))[0] if nxt_undo else "",
            "redoLabel": describe_step(db, source, json.loads(nxt_redo[0]))[0] if nxt_redo else ""}


def align_words(a, b):
    # Edit-distance alignment of two word lists, as difflib-style opcodes
    # (equal / replace / delete / insert, merged into runs). Words are
    # (word as written, less case and edge punctuation; its letters):
    # the same as written costs 0, the same letters written differently
    # ("Clawdrey" / "C-L-A-W-D-R-E-Y") 0.5, both "equal"; another word, in
    # place, 1 ("replace"); cutting or inserting one, 1. Ties prefer the
    # pairing, then a change in place. Unlike difflib's longest
    # matching runs, this never trades a respelling for a cut plus an insert
    # when the respelling looks like a neighbour ("Quadri, C-L-A-W-D-R-E-Y"
    # → "Clawdrey, C-L-A-W-D-R-E-Y"). Quadratic: for a paragraph, not a
    # whole transcript.
    def sub(x, y):
        return 0 if x[0] == y[0] else 0.5 if x[1] == y[1] else 1

    n, m = len(a), len(b)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j - 1] + sub(a[i - 1], b[j - 1]), d[i - 1][j] + 1, d[i][j - 1] + 1)
    steps, i, j = [], n, m
    while i or j:
        if i and j and d[i][j] == d[i - 1][j - 1] + sub(a[i - 1], b[j - 1]):
            steps.append("equal" if sub(a[i - 1], b[j - 1]) < 1 else "replace")
            i, j = i - 1, j - 1
        elif i and d[i][j] == d[i - 1][j] + 1:
            steps.append("delete")
            i -= 1
        else:
            steps.append("insert")
            j -= 1
    ops, i, j = [], 0, 0
    for op in reversed(steps):
        di, dj = (0, 1) if op == "insert" else (1, 0) if op == "delete" else (1, 1)
        if ops and ops[-1][0] == op:
            ops[-1][2] += di
            ops[-1][4] += dj
        else:
            ops.append([op, i, i + di, j, j + dj])
        i, j = i + di, j + dj
    return [tuple(o) for o in ops]


def apply_text_edit(db, source, text, para=None):
    # Diff the edited text against the kept words, word by word, on their
    # letters (so punctuation alone never makes a cut):
    # - deleted words are cut;
    # - words replaced one for one are corrections: shown changes, time stays;
    # - inserted words that match cut words between the same kept neighbors
    #   are restored; other inserted words have no audio and are ignored.
    # With para, the text is that paragraph alone, compared with its words.
    import difflib
    only = "" if para is None else " AND para = ?"
    scope = () if para is None else (para,)
    kept = db.execute("SELECT id, i, coalesce(shown, text), text FROM words "
                      f"WHERE source = ? AND kept = 1{only} ORDER BY i", (source, *scope)).fetchall()
    new = text.split()
    a, b = [word_key(w[2]) for w in kept], [word_key(w) for w in new]
    changes = {"cut": [], "restored": [], "corrected": []}
    ignored = 0

    def correct(word, typed):
        if typed != word[2]:
            changes["corrected"].append([word[0], word[2] if word[2] != word[3] else None,
                                         None if typed == word[3] else typed])

    def restore(at, typed):
        # Cut words between kept[at - 1] and kept[at], matched to typed words.
        nonlocal ignored
        lo = kept[at - 1][1] if at > 0 else -1
        hi = kept[at][1] if at < len(kept) else 1 << 30
        gone = db.execute(f"SELECT id, text FROM words WHERE source = ? AND kept = 0 AND i > ? AND i < ?{only} "
                          "ORDER BY i", (source, lo, hi, *scope)).fetchall()
        m = difflib.SequenceMatcher(None, [word_key(g[1]) for g in gone], [word_key(t) for t in typed], autojunk=False)
        matched = 0
        for blk in m.get_matching_blocks():
            changes["restored"] += [g[0] for g in gone[blk.a:blk.a + blk.size]]
            matched += blk.size
        ignored += len(typed) - matched

    # A paragraph (what the editor sends) is aligned by edit distance
    # (align_words); the whole transcript, too long for that, by difflib.
    def written(w):
        return (w.lower().strip(".,;:!?\"'()[]—–…"), word_key(w))

    ops = align_words([written(w[2]) for w in kept], [written(w) for w in new]) if para is not None else \
        difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            for k in range(i2 - i1):
                correct(kept[i1 + k], new[j1 + k])
        elif op == "delete":
            changes["cut"] += [w[0] for w in kept[i1:i2]]
        elif op == "replace" and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                correct(kept[i1 + k], new[j1 + k])
        elif op == "replace":
            changes["cut"] += [w[0] for w in kept[i1:i2]]
            restore(i1, new[j1:j2])
        else:  # insert
            restore(i1, new[j1:j2])
    record_text_change(db, source, changes)
    return {"cut": len(changes["cut"]), "restored": len(changes["restored"]),
            "corrected": len(changes["corrected"]), "ignored": ignored}


def replace_words(db, source, before, after):
    # The Workspace's Replace (podcast-search-replace): every run of kept
    # words reading `before` (whole words, any case) is corrected to `after`
    # as typed, word for word, so each keeps its time and audio, as a
    # correction typed into a paragraph does. Punctuation and a possessive
    # stay: "Quadri," -> "Clawdrey,", "Quadri's" -> "Clawdrey's". One Undo
    # step for all of it. Cut words are left alone.
    bw, aw = before.split(), after.split()
    if not bw:
        return {"error": "Type what to replace."}
    if len(bw) != len(aw):
        return {"error": "Before and after need the same number of words, so each keeps its time."}
    pats = [re.compile(r"^(\W*)" + re.escape(w) + r"(?!\w)(.*)$", re.I | re.S) for w in bw]
    kept = db.execute("SELECT id, coalesce(shown, text), text, para FROM words "
                      "WHERE source = ? AND kept = 1 ORDER BY i", (source,)).fetchall()
    corrected, paras, i = [], set(), 0
    while i <= len(kept) - len(bw):
        ms = [p.match(kept[i + k][1]) for k, p in enumerate(pats)]
        if not all(ms):
            i += 1
            continue
        for k, m in enumerate(ms):
            word_id, shown, text, para = kept[i + k]
            typed = m.group(1) + aw[k] + m.group(2)
            if typed != shown:
                corrected.append([word_id, shown if shown != text else None, None if typed == text else typed])
                paras.add(para)
        i += len(bw)
    if corrected:
        record_text_change(db, source, {"cut": [], "restored": [], "corrected": corrected,
                                        "before": before, "after": after}, reason="replace")
    return {"replaced": len(corrected), "paragraphs": len(paras)}


def record_text_change(db, source, changes, reason="edit"):
    # Apply {cut, restored, corrected: [[id, old shown, new shown]]} and keep
    # it as one Undo step. Nothing to do records nothing.
    if not any(changes.values()):
        return False
    # What each restored word was cut as, so Undo cuts it back the same way.
    changes["restoredWas"] = {str(i): r for i, r in db.execute(
        f"SELECT id, coalesce(cut_reason, 'edit') FROM words WHERE id IN ({','.join('?' * len(changes['restored']))})",
        changes["restored"])} if changes["restored"] else {}
    db.executemany("UPDATE words SET kept = 0, cut_reason = ? WHERE id = ?", [(reason, i) for i in changes["cut"]])
    db.executemany("UPDATE words SET kept = 1, cut_reason = NULL WHERE id = ?", [(i,) for i in changes["restored"]])
    db.executemany("UPDATE words SET shown = ? WHERE id = ?", [(new, i) for i, _, new in changes["corrected"]])
    db.execute("DELETE FROM transcript_edits WHERE source = ? AND undone = 1", (source,))
    db.execute("INSERT INTO transcript_edits (source, created_at, changes) VALUES (?, ?, ?)",
               (source, datetime_now(), json.dumps({**changes, "reason": reason})))
    return True


def undo_text_edit(db, source):
    row = db.execute("SELECT id, changes FROM transcript_edits WHERE source = ? AND undone = 0 "
                     "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
    if not row:
        return None
    ch = json.loads(row[1])
    db.executemany("UPDATE words SET kept = 1, cut_reason = NULL WHERE id = ?", [(i,) for i in ch["cut"]])
    was = ch.get("restoredWas", {})
    db.executemany("UPDATE words SET kept = 0, cut_reason = ? WHERE id = ?",
                   [(was.get(str(i), "edit"), i) for i in ch["restored"]])
    db.executemany("UPDATE words SET shown = ? WHERE id = ?", [(old, i) for i, old, _ in ch["corrected"]])
    # A tag step: each word's speaker as it was before (another speaker's
    # tag, a match, or none), so Undo is exact.
    db.executemany("UPDATE words SET speaker_id = ?, speaker_how = ?, speaker_unsure = ? WHERE id = ?",
                   [(t["was"][0], t["was"][1], t["was"][2], t["id"]) for t in ch.get("tagged", [])])
    db.execute("UPDATE transcript_edits SET undone = 1 WHERE id = ?", (row[0],))
    return ch


def redo_text_edit(db, source):
    # The earliest undone change, applied again as it was first made.
    row = db.execute("SELECT id, changes FROM transcript_edits WHERE source = ? AND undone = 1 "
                     "ORDER BY id LIMIT 1", (source,)).fetchone()
    if not row:
        return None
    ch = json.loads(row[1])
    db.executemany("UPDATE words SET kept = 0, cut_reason = ? WHERE id = ?",
                   [(ch.get("reason", "edit"), i) for i in ch["cut"]])
    db.executemany("UPDATE words SET kept = 1, cut_reason = NULL WHERE id = ?", [(i,) for i in ch["restored"]])
    db.executemany("UPDATE words SET shown = ? WHERE id = ?", [(new, i) for i, _, new in ch["corrected"]])
    db.executemany("UPDATE words SET speaker_id = ?, speaker_how = ?, speaker_unsure = ? WHERE id = ?",
                   [(t["now"][0], t["now"][1], t["now"][2], t["id"]) for t in ch.get("tagged", [])])
    db.execute("UPDATE transcript_edits SET undone = 0 WHERE id = ?", (row[0],))
    return ch


def elide_tics(db, source, dry=False, para=None):
    # Cut the tics still kept, as one Undo step (cut_reason 'tic'): every
    # filler, and every stutter (a word said again at once, the last kept),
    # except deliberate doubles (repeatsToKeep) and numbers. Words you've cut
    # are left as they are. Matched on letters, so "uh," is "uh". With dry,
    # only says what it would cut (and which words: ids); with para, only in
    # that paragraph (stutters are still found across its edges).
    conf = podcast_settings()
    # A filler can be a phrase ("you know"): its words, matched as
    # consecutive kept words (words already cut between them don't count),
    # longest phrase first. A match cuts all its words.
    phrases = sorted({tuple(k for k in map(word_key, f.split()) if k) for f in conf["fillers"].split(",")} - {()},
                     key=len, reverse=True)
    keep = {word_key(w) for w in conf["repeatsToKeep"].split(",") if word_key(w)}
    kept = db.execute("SELECT id, coalesce(shown, text) FROM words WHERE source = ? AND kept = 1 ORDER BY i",
                      (source,)).fetchall()
    keys = [word_key(w) for _, w in kept]
    groups, n = [], 0  # each filler match: the indices (into kept) of its words
    while n < len(keys):
        hit = next((p for p in phrases if tuple(keys[n:n + len(p)]) == p), None)
        if hit:
            groups.append(list(range(n, n + len(hit))))
            n += len(hit)
        else:
            n += 1
    in_filler = {m for g in groups for m in g}
    # Stutters on the words left once fillers go, so "and, uh, and" counts.
    rest = [(n, k) for n, k in enumerate(keys) if n not in in_filler and k]
    stutters = [m for m in range(len(rest) - 1)
                if rest[m][1] == rest[m + 1][1] and rest[m][1] not in keep and not rest[m][1].isdigit()]
    if para is not None:
        # A phrase belongs to the paragraph its first word is in.
        mine = {r[0] for r in db.execute("SELECT id FROM words WHERE source = ? AND para = ?", (source, para))}
        groups = [g for g in groups if kept[g[0]][0] in mine]
        stutters = [m for m in stutters if kept[rest[m][0]][0] in mine]
    filler_ids = [kept[m][0] for g in groups for m in g]
    stutter_ids = [kept[rest[m][0]][0] for m in stutters]
    found = {"fillers": len(groups), "stutters": len(stutter_ids), "ids": filler_ids + stutter_ids,
             "examples": [" ".join(kept[m][1] for m in g) for g in groups][:5]
             + [kept[rest[m][0]][1] + " " + kept[rest[m + 1][0]][1] for m in stutters][:5]}
    if not dry:
        record_text_change(db, source, {"cut": filler_ids + stutter_ids, "restored": [], "corrected": []},
                           reason="tic")
    return found


def silences(source):
    # Quiet stretches in the transcript's 16 kHz audio, [start, end] seconds,
    # measured once (ffmpeg silencedetect) and kept beside the transcript.
    folder = transcript_folder(source)
    cache = os.path.join(folder, "silences.json")
    wav = os.path.join(folder, "audio-16k.wav")
    if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(wav):
        return json.load(open(cache))
    out = subprocess.run(["ffmpeg", "-nostdin", "-i", wav, "-af", "silencedetect=noise=-35dB:d=0.08",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    found, start = [], None
    for m in re.finditer(r"silence_(start|end): ([\d.]+)", out):
        if m.group(1) == "start":
            start = float(m.group(2))
        elif start is not None:
            found.append([start, float(m.group(2))])
            start = None
    json.dump(found, open(cache, "w"))
    return found


def snap(t, quiet, lo, hi):
    # The middle of the quiet stretch nearest t (within SNAP_WINDOW), so a cut
    # keeps half of each pause around it: one natural pause at the join. Kept
    # inside [lo, hi], the neighboring kept words' edges.
    best = None
    for s, e in quiet:
        if e < t - SNAP_WINDOW or s > t + SNAP_WINDOW:
            continue
        mid = (s + e) / 2
        if best is None or abs(mid - t) < abs(best - t):
            best = mid
    return min(max(best if best is not None else t, lo), hi)


def aligned_edges(db, source, boundaries):
    # {i: (t, method)} for the boundaries between word i-1 and word i, by the
    # sound (align.py), cached in word_edges so each is aligned once. Without
    # the speech tools, or for a boundary align.py couldn't place, there is
    # no entry and the caller snaps by whisper's times instead.
    # Edges are cached per pauseMs setting (method "ctc@80"): a new setting
    # aligns them again; rows from before the setting existed are ignored.
    pause = podcast_settings()["pauseMs"]
    with ALIGN_LOCK:  # one alignment at a time: a second asker waits, then finds them cached
        return _aligned_edges(db, source, boundaries, pause)


def _aligned_edges(db, source, boundaries, pause):
    have = {i: (t, m.split("@")[0]) for i, t, m in db.execute(
        f"SELECT i, t, method FROM word_edges WHERE source = ? AND method LIKE ? "
        f"AND i IN ({','.join('?' * len(boundaries))})", [source, f"%@{pause}", *boundaries])} if boundaries else {}
    missing = sorted(set(boundaries) - set(have))
    if missing and os.path.isfile(SPEECH_PYTHON) and os.path.isdir(CTC_MODEL):
        t_start = time.time()
        wav = os.path.join(transcript_folder(source), "audio-16k.wav")
        r = subprocess.run([SPEECH_PYTHON, os.path.join(HERE, "align.py"), wav, DB, source, *map(str, missing)],
                           capture_output=True, text=True, env={**os.environ, "ALIGN_PAUSE_MS": str(pause)})
        found = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
        db.executemany("INSERT OR REPLACE INTO word_edges (source, i, t, method) VALUES (?, ?, ?, ?)",
                       [(source, e["i"], e["t"], f"{e['method']}@{pause}") for e in found])
        db.commit()
        have.update({e["i"]: (e["t"], e["method"]) for e in found})
        methods = collections.Counter(e["method"] for e in found)
        print(f"align source={source} edges={len(missing)} {dict(methods)} secs={time.time() - t_start:.1f}"
              + (f" error={r.stderr.strip().splitlines()[-1]}" if r.returncode else ""), flush=True)
    return {i: v for i, v in have.items() if v[1] != "unaligned"}


def text_cuts(db, source, duration):
    # The cut words as source spans: a run of cut words with no kept word
    # between them is one span. Each edge is placed by the sound
    # (aligned_edges); one that can't be is snapped to quiet by whisper's
    # times and the span is marked unaligned. Then what is kept, as source
    # intervals with where each starts on the edited timeline, and the last
    # round-trip check's verdict on each span (check_text).
    rows = db.execute("SELECT id, i, t0, t1, coalesce(shown, text), kept, cut_reason FROM words "
                      "WHERE source = ? ORDER BY i", (source,)).fetchall()
    runs, k = [], 0
    while k < len(rows):
        if rows[k][5]:
            k += 1
            continue
        j = k
        while j + 1 < len(rows) and not rows[j + 1][5]:
            j += 1
        runs.append((k, j))
        k = j + 1
    edges = aligned_edges(db, source, [rows[k][1] for k, _ in runs if k > 0]
                          + [rows[j + 1][1] for _, j in runs if j + 1 < len(rows)])
    quiet = None
    try:
        verdicts = json.load(open(os.path.join(transcript_folder(source), "check.json")))["spans"]
    except (OSError, ValueError, KeyError):
        verdicts = {}
    spans = []
    for k, j in runs:
        unaligned = False
        if k == 0:
            a = 0.0
        elif rows[k][1] in edges:
            a = edges[rows[k][1]][0]
        else:
            quiet = quiet if quiet is not None else silences(source)
            a, unaligned = snap(rows[k][2], quiet, rows[k - 1][3], rows[k][2]), True
        if j + 1 == len(rows):
            b = duration
        elif rows[j + 1][1] in edges:
            b = edges[rows[j + 1][1]][0]
        else:
            quiet = quiet if quiet is not None else silences(source)
            b, unaligned = snap(rows[j][3], quiet, rows[j][3], rows[j + 1][2]), True
        reasons = sorted({r[6] or "edit" for r in rows[k:j + 1]})
        spans.append({"ids": [r[0] for r in rows[k:j + 1]], "text": " ".join(r[4] for r in rows[k:j + 1]),
                      "t0": round(a, 3), "t1": round(max(a, b), 3), "reason": "+".join(reasons),
                      "unaligned": unaligned, "check": verdicts.get(str(rows[k][0]))})
    kept, at, out = [], 0.0, 0.0
    for s in spans:
        if s["t0"] > at:
            kept.append({"src0": round(at, 3), "src1": s["t0"], "out0": round(out, 3)})
            out += s["t0"] - at
        at = max(at, s["t1"])
    if at < duration:
        kept.append({"src0": round(at, 3), "src1": round(duration, 3), "out0": round(out, 3)})
        out += duration - at
    try:
        last = json.load(open(os.path.join(transcript_folder(source), "check.json")))
        check = {k: last[k] for k in ("at", "range", "joins", "suspect", "noise")}
    except (OSError, ValueError, KeyError):
        check = None
    return {"spans": spans, "kept": kept, "keptSecs": round(out, 3), "cutSecs": round(duration - out, 3),
            "unaligned": sum(1 for s in spans if s["unaligned"]), "check": check}


PARA_AUDIO = os.path.join(ROOT, ".preview", "paras")  # rendered paragraphs, served at /.preview/paras/


def para_pieces(db, source, mine, duration):
    # What a paragraph's audio is made of (para_audio): its stretch of the
    # source, from the cut edge before its first word (mine: its words' i,
    # in order) to the one after its last, aligned like any cut edge, less
    # the cuts in it. Returns (start, end, pieces), pieces as source (a, b).
    words = db.execute("SELECT i, t0, t1, kept FROM words WHERE source = ? ORDER BY i", (source,)).fetchall()
    first, last = mine[0], mine[-1]
    edges = aligned_edges(db, source, [b for b in (first, last + 1) if 0 < b < len(words)])

    def edge(b):
        if b <= 0:
            return 0.0
        if b >= len(words):
            return duration
        return edges[b][0] if b in edges else (words[b - 1][2] + words[b][1]) / 2

    start, end = edge(first), edge(last + 1)
    pieces = [(max(k["src0"], start), min(k["src1"], end)) for k in text_cuts(db, source, duration)["kept"]
              if k["src1"] > start and k["src0"] < end]
    return start, end, [(a, b) for a, b in pieces if b - a > 0.02]


def para_marks(db, source, para, duration):
    # Where each kept word of a paragraph is, in its text as the editor shows
    # it (kept words, corrected, joined by spaces, as paragraphs() makes it)
    # and in its rendered audio (para_audio's pieces, end to end): a list of
    # [character offset, seconds]. The editor moves the paragraph's player to
    # the word the caret lands on.
    rows = db.execute("SELECT i, t0, kept, coalesce(shown, text) FROM words WHERE source = ? AND para = ? "
                      "ORDER BY i", (source, para)).fetchall()
    if not any(r[2] for r in rows):
        return []
    _, _, pieces = para_pieces(db, source, [r[0] for r in rows], duration)
    before, out = [], 0.0
    for a, b in pieces:
        before.append(out)
        out += b - a
    marks, c = [], 0
    for _, t0, kept, w in rows:
        if not kept:
            continue
        # The piece holding the word's start; a start just before its piece
        # (an edge placed by the sound) counts as the piece's start.
        t = next((before[n] + max(0.0, t0 - a) for n, (a, b) in enumerate(pieces) if t0 < b), out)
        marks.append([c, round(t, 3)])
        c += len(w) + 1
    return marks


def audio_versions(db, source):
    # Each paragraph's sound version: a hash of which of its words are kept,
    # all its render depends on besides the settings in its file name. Not
    # paragraphs()' version, which also covers corrections and speakers, so
    # that tagging or matching voices neither re-renders a paragraph nor makes
    # the final video look out of date. {para: version}, paragraphs with any
    # word kept only.
    h, kept = {}, set()
    for para, wid, k in db.execute("SELECT para, id, kept FROM words WHERE source = ? ORDER BY i", (source,)):
        h.setdefault(para, hashlib.sha1()).update(f"{wid}:{k};".encode())
        if k:
            kept.add(para)
    return {p: x.hexdigest()[:10] for p, x in h.items() if p in kept}


def para_audio(db, source, para, duration):
    # A paragraph as edited, rendered from the source movie's own soundtrack:
    # its stretch of the source (from the cut edge before its first word to
    # the one after its last, aligned like any cut edge) less the cuts in it,
    # 10 ms fades at each join. The video is cut from the same pieces (hard
    # cuts, no fades), so one .mp4 is both the card's audio and the left
    # pane's video, on one timeline. Named by the paragraph's sound version
    # (audio_versions), so it is rendered once per change to its cuts; older renders of it are removed. Returns the
    # file name, or None when every word is cut.
    mine = [w[0] for w in db.execute("SELECT i FROM words WHERE source = ? AND para = ? ORDER BY i", (source, para))]
    if not mine:
        return None
    version = audio_versions(db, source).get(para)
    if version is None:
        return None
    conf = podcast_settings()
    fade = conf["fadeMs"] / 1000
    stem = re.sub(r"[^\w.-]+", "_", os.path.splitext(source)[0])
    # The settings that shape the render are in its name, so changing one
    # renders anew.
    name = f"{stem}-p{para}-{version}-a{conf['pauseMs']}-f{conf['fadeMs']}.mp4"
    path = os.path.join(PARA_AUDIO, name)
    if os.path.isfile(path):
        return name
    start, end, pieces = para_pieces(db, source, mine, duration)
    if not pieces:
        return None
    graph = ";".join(f"[0:v]trim={a - start:.3f}:{b - start:.3f},setpts=PTS-STARTPTS[v{n}];"
                     + f"[0:a]atrim={a - start:.3f}:{b - start:.3f},asetpts=PTS-STARTPTS"
                     + (f",afade=t=in:d={fade},afade=t=out:st={max(b - a - fade, 0):.3f}:d={fade}" if fade else "")
                     + f"[p{n}]" for n, (a, b) in enumerate(pieces))
    graph += ";" + "".join(f"[v{n}][p{n}]" for n in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=1[vout][out]"
    os.makedirs(PARA_AUDIO, exist_ok=True)
    t_start = time.time()
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                        "-i", source_paths()[source], "-filter_complex", graph, "-map", "[vout]", "-map", "[out]",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", path + ".tmp.mp4"],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("render: " + (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
    os.replace(path + ".tmp.mp4", path)
    for old in os.listdir(PARA_AUDIO):
        if old.startswith(f"{stem}-p{para}-") and old != name:
            os.remove(os.path.join(PARA_AUDIO, old))
    print(f"para audio source={source} para={para} pieces={len(pieces)} video=1 "
          f"secs={sum(b - a for a, b in pieces):.1f} render={time.time() - t_start:.2f}", flush=True)
    return name


def para_poster(db, source, para, duration):
    # A still of a paragraph's clip, its first frame, for the Workspace
    # video's poster: shown at the video's size from the moment it mounts or
    # changes clip, instead of an empty black box until the clip loads
    # (workspace-video-steady). Beside the clip, named after it, so a
    # re-render of the paragraph removes it with the old clip. Returns the
    # file name, or None when every word is cut.
    name = para_audio(db, source, para, duration)
    if not name:
        return None
    jpg = name + ".jpg"
    path = os.path.join(PARA_AUDIO, jpg)
    if not os.path.isfile(path):
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", os.path.join(PARA_AUDIO, name),
                            "-frames:v", "1", "-q:v", "4", path + ".tmp.jpg"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("poster: " + (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        os.replace(path + ".tmp.jpg", path)
    return jpg


finals = {}  # source -> {"done", "total"} while its final video is made (make_final)
FINAL_LOCK = threading.Lock()


def final_name(source):
    # The final video's name in media/exports/, without extension: the
    # project's folder name, and the source's when it differs (a project with
    # several sources has a final per source).
    slug = re.sub(r"[^\w.-]+", "_", os.path.basename(os.path.normpath(studio_paths.PROJECT)))
    stem = re.sub(r"[^\w.-]+", "_", os.path.splitext(source)[0])
    return (slug if stem == slug else f"{slug}-{stem}") + "-final"


# Every final video and final transcript made is kept, numbered v1, v2...,
# in media/exports/finals/: <name>-v<N>.mp4 and .json (the video's clips),
# <name>-transcript-v<N>.md and .json (a snapshot of every kept word's
# speaker and time, the turns, the samples then, the video version it is
# timed by). A snapshot, because the stored voice matches change with every
# Remake: it is what two transcripts are compared by (compare_transcripts).
# The newest video is also linked as media/exports/<name>.mp4, the one the
# Exports page lists.
def finals_dir():
    return os.path.join(ROOT, "exports", "finals")


def final_url(rel):
    return f"http://127.0.0.1:{PORT}/exports/finals/{urllib.parse.quote(rel)}"


def versions(source, kind):
    # [(n, info)] newest first; kind "video" or "transcript".
    base = final_name(source) + ("-transcript" if kind == "transcript" else "")
    pat = re.compile(re.escape(base) + r"-v(\d+)\.json$")
    out = []
    for name in os.listdir(finals_dir()) if os.path.isdir(finals_dir()) else []:
        m = pat.match(name)
        if m:
            try:
                out.append((int(m.group(1)), json.load(open(os.path.join(finals_dir(), name)))))
            except (OSError, ValueError):
                pass
    return sorted(out, key=lambda v: -v[0])


def next_version(source, kind):
    vs = versions(source, kind)
    return (vs[0][0] if vs else 0) + 1


def kept_paras(db, source):
    # The paragraphs with kept words, in order: what the final video is made of.
    return [r[0] for r in db.execute("SELECT para FROM words WHERE source = ? GROUP BY para "
                                     "HAVING sum(kept) > 0 ORDER BY min(i)", (source,))]


def scenes_key(db):
    # What a screencast's final video is made of, now: every scene in list
    # order, its take file's size and time, and the state of its items
    # (overlay.APPLIED_SQL). Export records it beside each export
    # (.<name>.scenes.json); a different key means the scenes have changed
    # since.
    h = hashlib.sha1()
    for tid, f in db.execute("SELECT id, file FROM takes ORDER BY position, created_at DESC").fetchall():
        try:
            st = os.stat(os.path.join(ROOT, f))
            stat = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            stat = "-"
        applied = db.execute(overlay.APPLIED_SQL, (tid,)).fetchone()[0]
        h.update(f"{tid}:{f}:{stat}:{applied};".encode())
    return h.hexdigest()[:12]


def screencast_finals(db):
    # GET /screencast/final: the exports Studio made (kind "export"),
    # numbered v1, v2... by age and listed newest first, each with whether
    # it matches the scenes now (null for one made before Export kept a
    # record). The newest's fields also at the top level, as /podcast/final.
    outdir = os.path.join(ROOT, "exports")
    files = studio_files()
    found = []
    for name in os.listdir(outdir) if os.path.isdir(outdir) else []:
        path = os.path.join(outdir, name)
        if name.startswith(".") or not name.endswith("-takes.mp4") or not os.path.isfile(path):
            continue
        if studio_kind(name, os.stat(path), files) != "export":
            continue
        found.append((os.path.getmtime(path), name, path))
    found.sort()
    key = scenes_key(db)
    vs = []
    # A version's number is fixed when it's made (its record's n), so
    # deleting one never renumbers the rest; an export from before records
    # gets the next number by age, saved the first time it's listed.
    k = 0
    for mtime, name, path in found:
        try:
            rec = json.load(open(os.path.join(outdir, f".{name}.scenes.json")))
        except (OSError, ValueError):
            rec = {}
        if not rec.get("n"):
            # The first listing fixes an older export's number for good:
            # saved in its record, with no scenes (so it has no current/out
            # of date mark).
            rec["n"] = max(k, max((v["n"] for v in vs), default=0)) + 1
            try:
                with open(os.path.join(outdir, f".{name}.scenes.json"), "w") as fh:
                    json.dump(rec, fh)
            except OSError:
                pass
        k = n = rec["n"]
        secs = rec.get("secs")
        if secs is None:
            try:
                secs = round(float(probe(path, "format=duration") or 0), 1)
            except Exception:
                secs = 0
        vs.append({"n": n, "name": name, "url": f"http://127.0.0.1:{PORT}/exports/{urllib.parse.quote(name)}",
                   "secs": secs, "madeAt": rec.get("madeAt") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                   "current": (rec["scenes"] == key) if "scenes" in rec else None})
    vs.reverse()
    # Every other video in media/exports/ (edited elsewhere, leveled), so the
    # Exports tab lists each file once, in one picker; and every file's note
    # (screencast-exports-coherent).
    listed = list_exports()
    notes = {e["name"]: e.get("note", "") for e in listed["exports"] + listed["leveled"]}
    for v in vs:
        v["note"], v["where"] = notes.get(v["name"], ""), "exports"
    names = {v["name"] for v in vs}
    others = [{"name": e["name"], "url": e["url"], "secs": e["seconds"], "madeAt": e["created"],
               "note": e.get("note", ""), "where": e["where"], "leveled": e in listed["leveled"]}
              for e in listed["exports"] + listed["leveled"] if e["name"] not in names]
    others.sort(key=lambda e: e["madeAt"], reverse=True)
    out = {"making": {"exporting": True} if exporting.locked() else None, "versions": vs, "exists": bool(vs),
           "others": others}
    if vs:
        out.update({k: vs[0][k] for k in ("n", "url", "secs", "madeAt", "current")})
    return out


def link_latest_video(source):
    # media/exports/<name>.mp4 is the newest video version (a hard link: no
    # second copy), or nothing once every version is deleted.
    base = final_name(source)
    top = os.path.join(ROOT, "exports", base + ".mp4")
    vs = versions(source, "video")
    try:
        os.remove(top)
    except OSError:
        pass
    forget_studio_file(base + ".mp4")
    if vs:
        src = os.path.join(finals_dir(), f"{base}-v{vs[0][0]}.mp4")
        tmp = os.path.join(ROOT, "exports", f".{base}.link.mp4")
        try:
            os.remove(tmp)
        except OSError:
            pass
        os.link(src, tmp)
        record_studio_file(base + ".mp4", tmp, "export")
        os.replace(tmp, top)


def adopt_unversioned(db, source):
    # Finals made before there were versions (2026-11-14, a few minutes'
    # worth): media/exports/<name>.json and .mp4 become video v1, and
    # <name>-transcript.md becomes transcript v1, its snapshot taken from the
    # matches stored now (still that run's: only a Remake changes them).
    base = final_name(source)
    old_json = os.path.join(ROOT, "exports", base + ".json")
    if not os.path.isfile(old_json) or versions(source, "video"):
        return
    os.makedirs(finals_dir(), exist_ok=True)
    info = json.load(open(old_json))
    info["n"] = 1
    os.link(os.path.join(ROOT, "exports", base + ".mp4"), os.path.join(finals_dir(), f"{base}-v1.mp4"))
    with open(os.path.join(finals_dir(), f"{base}-v1.json"), "w") as f:
        json.dump(info, f)
    os.remove(old_json)
    old_md = os.path.join(ROOT, "exports", base + "-transcript.md")
    had_md = os.path.isfile(old_md)
    if had_md:
        made = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(old_md)))
        save_transcript_version(db, source, made_at=made)
        os.remove(old_md)
    print(f"final adopted source={source} video=v1 transcript={'v1' if had_md else '-'}",
          flush=True)


def make_final(source):
    # The background half of POST /podcast/final: every paragraph with kept
    # words rendered as the cards play it (para_audio, cached ones reused),
    # joined end to end with a stream copy (the clips share one encoding)
    # into the next video version. Its .json: each clip's paragraph, sound
    # version and start in the video, which says whether the edit has
    # changed since (final_state) and times the transcript's words. Stream
    # copy, not one pass over the source with every cut: the joins are the
    # cards' own, and 156 cached clips join in seconds (2026-11-13: 39:28,
    # 1.15 s over text_cuts' keptSecs).
    t_start = time.time()
    try:
        db = sqlite3.connect(DB)
        duration = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()[0] or 0
        order = kept_paras(db, source)
        stem = re.sub(r"[^\w.-]+", "_", os.path.splitext(source)[0])
        clips = []
        for n, para in enumerate(order):
            with TEXT_LOCK:
                name = para_audio(db, source, para, duration)
            if name:
                version = name[len(f"{stem}-p{para}-"):].split("-")[0]
                path = os.path.join(PARA_AUDIO, name)
                secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                             "-of", "csv=p=0", path], capture_output=True, text=True,
                                            check=True).stdout)
                clips.append({"para": para, "v": version, "path": path, "secs": secs})
            finals[source] = {"done": n + 1, "total": len(order)}
            push({"final": source, **finals[source]})
        t_render = time.time()
        os.makedirs(finals_dir(), exist_ok=True)
        base = final_name(source)
        num = next_version(source, "video")
        part = os.path.join(finals_dir(), f".{base}-v{num}.part.mp4")
        lst = os.path.join(finals_dir(), f".{base}.list.txt")
        with open(lst, "w") as f:
            for c in clips:
                f.write("file '" + c["path"].replace("'", "'\\''") + "'\n")
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", "-movflags", "+faststart", part], capture_output=True, text=True)
        os.remove(lst)
        if r.returncode:
            raise RuntimeError("join: " + (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        at = 0.0
        for c in clips:
            c["at"] = round(at, 3)
            at += c["secs"]
            del c["path"]
        os.replace(part, os.path.join(finals_dir(), f"{base}-v{num}.mp4"))
        with open(os.path.join(finals_dir(), f".{base}-v{num}.json.tmp"), "w") as f:
            json.dump({"n": num, "source": source, "madeAt": datetime_now(), "secs": round(at, 3),
                       "clips": clips}, f)
        os.replace(os.path.join(finals_dir(), f".{base}-v{num}.json.tmp"),
                   os.path.join(finals_dir(), f"{base}-v{num}.json"))
        link_latest_video(source)
        print(f"final video source={source} v={num} paras={len(clips)} secs={at:.1f} "
              f"render={t_render - t_start:.1f} join={time.time() - t_render:.1f}", flush=True)
        push({"final": source, "finished": True, "n": num})
    except Exception as e:
        print(f"final video failed source={source} error={e}", flush=True)
        push({"final": source, "error": str(e)})
    finally:
        with FINAL_LOCK:
            finals.pop(source, None)
        notify("exports")


def is_current(now, info):
    # A video version is current while every kept paragraph's sound version
    # is the one it was made from.
    return now == {c["para"]: c["v"] for c in info["clips"]}


def final_state(db, source):
    # GET /podcast/final: the video versions, newest first, each with its
    # address, length, when, and (the newest) whether it's current. The
    # newest's fields also at the top level.
    adopt_unversioned(db, source)
    base = final_name(source)
    now = audio_versions(db, source)
    vs = []
    for n, info in versions(source, "video"):
        if os.path.isfile(os.path.join(finals_dir(), f"{base}-v{n}.mp4")):
            vs.append({"n": n, "url": final_url(f"{base}-v{n}.mp4"), "secs": info["secs"],
                       "madeAt": info["madeAt"], "current": is_current(now, info)})
    out = {"making": finals.get(source), "versions": vs, "exists": bool(vs)}
    if vs:
        out.update({k: vs[0][k] for k in ("n", "url", "secs", "madeAt", "current")})
    return out


def video_info(source, n):
    for m, info in versions(source, "video"):
        if m == n:
            return info
    return None


def final_words(db, source, info):
    # The kept words of the paragraphs in a video version, each with its
    # speaker as stored (your samples, then voiceprint matches) and its time
    # in that video (its clip's start plus its place in the clip,
    # para_marks): [[id, text, speaker, how, unsure, at]].
    duration = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()[0] or 0
    out = []
    for clip in info["clips"]:
        rows = db.execute("SELECT id, coalesce(shown, text), speaker_id, speaker_how, speaker_unsure FROM words "
                          "WHERE source = ? AND para = ? AND kept = 1 ORDER BY i", (source, clip["para"])).fetchall()
        marks = para_marks(db, source, clip["para"], duration)
        for (wid, w, spk, how, unsure), (_, t) in zip(rows, marks):
            out.append([wid, w, spk, how, 1 if (how != "tag" and (unsure or spk is None)) else 0,
                        round(clip["at"] + t, 2)])
    return out


def final_at(db, source, para):
    # Where paragraph para starts in the newest final video: {n, at}. A
    # paragraph that isn't in that video falls back to the nearest earlier
    # one that is; {n, at: None} when none is (or n None with no video).
    vs = versions(source, "video")
    if not vs:
        return {"n": None, "at": None}
    n, info = vs[0]
    earlier = [c for c in info["clips"] if c["para"] <= para]
    if not earlier:
        return {"n": n, "at": None}
    clip = max(earlier, key=lambda c: c["para"])
    duration = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()
    marks = para_marks(db, source, clip["para"], (duration[0] if duration else 0) or 0)
    if not marks:
        return {"n": n, "at": round(clip["at"], 2)}
    return {"n": n, "at": round(clip["at"] + marks[0][1], 2)}


def turns_of(words, names, colors):
    # Consecutive words with one speaker make a turn; a turn is unsure when
    # most of its words' matches were.
    turns = []
    for wid, w, spk, how, unsure, at in words:
        if not turns or turns[-1]["speaker"] != spk:
            turns.append({"speaker": spk, "name": names.get(str(spk), "Unknown"), "color": colors.get(str(spk)),
                          "at": at, "words": [], "u": 0, "w0": wid})
        turns[-1]["words"].append(w)
        turns[-1]["u"] += unsure
    return [{"speaker": t["speaker"], "name": t["name"], "color": t["color"], "at": t["at"], "w0": t["w0"],
             "text": " ".join(t["words"]), "unsure": t["u"] * 2 > len(t["words"])} for t in turns]


ROW_PARA_MIN = 120   # a long turn's row ends at a paragraph break once it has this many words
ROW_SENT_MIN = 250   # or at a sentence end once it has this many (a paragraph that runs long)
SENTENCE_END = re.compile(r"[.?!][\"')\]]*$")


def rows_of(turns, words, para_of):
    # The Final transcript page's rows: each turn (turns_of) cut into rows so
    # its List can virtualize (issue 5: before speaker matching a whole
    # recording is one turn of ~9,000 words). A row ends at a Workspace
    # paragraph break once it has ROW_PARA_MIN words, or at a sentence end
    # once it has ROW_SENT_MIN. words are the snapshot's (wid, w, spk, ...)
    # rows, grouped into turns as turns_of groups them; para_of maps a word
    # id to its paragraph. A row keeps its turn's speaker, name, color and
    # unsure, and has its own at, w0, para, text and wl, plus turn (the
    # turn's index), part (0, 1, ...) and i (its index in the result).
    rows = []
    k = -1
    prev = object()
    cur = None
    for wid, w, spk, _how, _unsure, at in words:
        if k < 0 or spk != prev:
            k, prev, cur = k + 1, spk, None
            part = 0
        elif cur is not None:
            n = len(cur["wl"])
            last = cur["wl"][-1]["w"]
            if (n >= ROW_PARA_MIN and para_of.get(wid) != para_of.get(cur["wl"][-1]["id"])) \
                    or (n >= ROW_SENT_MIN and SENTENCE_END.search(last)):
                cur = None
        if cur is None:
            t = turns[k]
            cur = {"speaker": t["speaker"], "name": t["name"], "color": t["color"], "unsure": t["unsure"],
                   "at": at, "w0": wid, "para": para_of.get(wid), "turn": k, "part": part, "i": len(rows), "wl": []}
            part += 1
            rows.append(cur)
        cur["wl"].append({"id": wid, "w": w})
    for r in rows:
        r["text"] = " ".join(x["w"] for x in r["wl"])
    return rows


def clock(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def voice_settings():
    # The Settings that shape who's speaking (voiceprint.py), recorded with
    # each transcript version: a change to one is a reason to Remake.
    # And the matcher itself (a hash of voiceprint.py): a new version of it
    # is a reason to Remake too.
    conf = podcast_settings()
    try:
        matcher = hashlib.sha1(open(os.path.join(HERE, "voiceprint.py"), "rb").read()).hexdigest()[:10]
    except OSError:
        matcher = None
    return {**{k: conf[k] for k in ("vpWindowS", "vpStepS", "vpMinTurnS", "vpMinTaggedS", "vpEdgeS")},
            "matcher": matcher}


def samples_now(db, source):
    # Each speaker's seconds of samples, {speaker id: secs}: what a
    # transcript version records, and what says a Remake would differ.
    return {str(spk): round(secs, 1) for spk, secs in db.execute(
        "SELECT speaker_id, sum(t1 - t0) FROM words WHERE source = ? AND speaker_how = 'tag' AND kept = 1 "
        "GROUP BY speaker_id", (source,))}


def save_transcript_version(db, source, made_at=None):
    # The next transcript version: a snapshot of the words' speakers and
    # times in the newest video version, its turns, and its Markdown.
    # Returns its number, or None with no video.
    vs = versions(source, "video")
    if not vs:
        return None
    vnum, info = vs[0]
    names = {str(i): n for i, n in db.execute("SELECT id, name FROM speakers")}
    colors = {str(i): c for i, c in db.execute("SELECT id, color FROM speakers")}
    samples = samples_now(db, source)
    words = final_words(db, source, info)
    turns = turns_of(words, names, colors)
    num = next_version(source, "transcript")
    base = final_name(source) + "-transcript"
    snap = {"n": num, "source": source, "madeAt": made_at or datetime_now(), "video": vnum,
            "videoSecs": info["secs"], "names": names, "colors": colors, "samples": samples,
            "settings": voice_settings(),
            "words": words, "turns": turns}
    lines = [f"# {os.path.splitext(source)[0]}: final transcript v{num}", "",
             f"Times are in final video v{vnum} ({clock(info['secs'])}, made {info['madeAt']}). "
             "A turn marked (?) is one where the voice match was unsure for most of its words.", "",
             "Samples: " + ", ".join(f"{names.get(k, k)} {v:g} s" for k, v in samples.items()), ""]
    for tr in turns:
        lines += [f"**{tr['name']}**{' (?)' if tr['unsure'] else ''} [{clock(tr['at'])}]", "", tr["text"], ""]
    os.makedirs(finals_dir(), exist_ok=True)
    for ext, body in ((".md", "\n".join(lines)), (".json", json.dumps(snap))):
        path = os.path.join(finals_dir(), f"{base}-v{num}{ext}")
        with open(path + ".tmp", "w") as f:
            f.write(body)
        os.replace(path + ".tmp", path)
    return num


TRANSCRIPT_HTML_STYLE = """
:root { --bg: #fff; --fg: #1d1f23; --muted: #5d6470; --rule: #e3e6ea; --link: #2a62c9;
  --grip: #c9ced6; --left: 45%; --now: #eef3fc; --said: #cfe0fb; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #16181c; --fg: #e8eaed; --muted: #9aa1ac; --rule: #2c3036; --link: #8ab4f8;
    --grip: #3a3f47; --now: #1f2a3a; --said: #2d4466; }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 17px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.app { display: grid; grid-template-columns: var(--left) 8px 1fr; height: 100vh; }
.left { overflow: auto; padding: 16px; }
.right { overflow: auto; padding: 0 20px 64px; }
.split { cursor: col-resize; background: var(--rule); touch-action: none; position: relative; }
.split::after { content: ""; position: absolute; top: 50%; left: 2px; width: 4px; height: 40px;
  margin-top: -20px; border-radius: 2px; background: var(--grip); }
.split:hover, .split:focus-visible, .split.drag { background: var(--grip); outline: none; }
.left { position: relative; }
.toggle { position: absolute; top: 14px; right: 16px; font: inherit; font-size: 0.8rem;
  color: var(--link); background: none; border: 1px solid var(--rule); border-radius: 6px;
  padding: 2px 10px; cursor: pointer; }
.toggle:hover { border-color: var(--link); }
.app.solo { grid-template-columns: 1fr; }
.app.solo .split, .app.solo .right { display: none; }
h1 { font-size: 1.2rem; margin: 0 0 4px; padding-right: 9em; }
.date { color: var(--muted); font-size: 0.9rem; margin: 0 0 8px; }
.summary { margin: 0 0 10px; font-size: 0.95rem; }
video { width: 100%; background: #000; display: block; border-radius: 6px; }
.turn { display: grid; grid-template-columns: 4.5em 1fr; gap: 0 12px; padding: 14px 0;
  border-bottom: 1px solid var(--rule); }
.turn a.t { color: var(--link); text-decoration: none; font-variant-numeric: tabular-nums;
  font-size: 0.9rem; padding-top: 2px; }
.turn a.t:hover { text-decoration: underline; }
.who { font-weight: 600; }
.unsure { color: var(--muted); font-weight: 400; }
.turn p { margin: 2px 0 0; }
.turn.now { background: var(--now); box-shadow: -20px 0 0 var(--now), 20px 0 0 var(--now); }
.s { cursor: pointer; border-radius: 3px; }
.s:hover { text-decoration: underline; text-decoration-color: var(--grip); }
.s.said { background: var(--said); }
@media (max-width: 720px) {
  .app { display: block; height: auto; }
  .split { display: none; }
  .left { position: sticky; top: 0; z-index: 1; background: var(--bg); border-bottom: 1px solid var(--rule); }
  .left video { max-height: 35vh; }
  .right { overflow: visible; padding: 0 16px 64px; }
  .turn { grid-template-columns: 1fr; }
}
"""

TRANSCRIPT_HTML_SCRIPT = """
document.addEventListener('click', function (e) {
  var a = e.target.closest('a.t, .s');
  if (!a) return;
  var v = document.getElementById('video');
  if (!v) return;
  e.preventDefault();
  v.currentTime = Number(a.dataset.t);
  var p = v.play();
  if (p && p.catch) p.catch(function () {});
});
(function () {
  var app = document.querySelector('.app'), split = document.querySelector('.split');
  var KEY = 'transcript-split', pct = null;
  function set(p) {
    pct = Math.min(80, Math.max(20, p));
    app.style.setProperty('--left', pct + '%');
    split.setAttribute('aria-valuenow', Math.round(pct));
  }
  try { var saved = Number(localStorage.getItem(KEY)); if (saved) set(saved); } catch (e) {}
  function save() { try { if (pct !== null) localStorage.setItem(KEY, String(pct)); } catch (e) {} }
  split.addEventListener('pointerdown', function (e) {
    split.setPointerCapture(e.pointerId); split.classList.add('drag'); e.preventDefault();
  });
  split.addEventListener('pointermove', function (e) {
    if (!split.hasPointerCapture(e.pointerId)) return;
    var r = app.getBoundingClientRect();
    set((e.clientX - r.left) / r.width * 100);
  });
  split.addEventListener('pointerup', function (e) {
    split.releasePointerCapture(e.pointerId); split.classList.remove('drag'); save();
  });
  split.addEventListener('keydown', function (e) {
    var now = pct === null ? 45 : pct;
    if (e.key === 'ArrowLeft') { set(now - 2); save(); e.preventDefault(); }
    if (e.key === 'ArrowRight') { set(now + 2); save(); e.preventDefault(); }
  });
  // Show / hide the transcript: the page's default (the project's
  // setting, data-show-transcript), unless this viewer chose otherwise on
  // this page (remembered per page address).
  var toggle = document.querySelector('.toggle'), SOLO = 'transcript-hidden:' + location.pathname;
  function solo(on) {
    app.classList.toggle('solo', on);
    toggle.textContent = on ? 'Show transcript' : 'Hide transcript';
    toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
  var hidden = app.dataset.showTranscript !== '1';
  try { var mine = localStorage.getItem(SOLO); if (mine !== null) hidden = mine === '1'; } catch (e) {}
  solo(hidden);
  toggle.addEventListener('click', function () {
    var on = !app.classList.contains('solo');
    solo(on);
    try { localStorage.setItem(SOLO, on ? '1' : '0'); } catch (e) {}
    cur = -1; follow();
  });
  // Follow the video: the turn it has reached is marked and put at the top
  // of the transcript, and stays there until the next turn starts.
  var v = document.getElementById('video'), left = document.querySelector('.left');
  var turns = Array.prototype.slice.call(document.querySelectorAll('.turn'));
  var times = turns.map(function (t) { return Number(t.querySelector('a.t').dataset.t); });
  var cur = -1, said = null, right = document.querySelector('.right');
  function last(list, t) {
    var k = -1, lo = 0, hi = list.length - 1;
    while (lo <= hi) { var m = (lo + hi) >> 1; if (list[m] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
    return k;
  }
  function pinned() { return getComputedStyle(left).position === 'sticky'; }
  // Stacked (narrow): the video sits pinned above, so stop below it.
  function margin(el) { el.style.scrollMarginTop = pinned() ? left.offsetHeight + 'px' : '0px'; }
  function follow() {
    if (app.classList.contains('solo') || !turns.length) return;
    var t = v.currentTime + 0.05, k = last(times, t);
    if (k !== cur) {
      if (cur >= 0) turns[cur].classList.remove('now');
      cur = k;
      if (k >= 0) {
        turns[k].classList.add('now');
        margin(turns[k]);
        turns[k].scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
    }
    // The sentence being spoken, within the turn: marked; scrolled to only
    // when it has gone below the visible part of the transcript (a long
    // turn), so the text doesn't move with every sentence.
    var s = null;
    if (k >= 0) {
      var spans = turns[k].querySelectorAll('.s');
      var j = last(Array.prototype.map.call(spans, function (x) { return Number(x.dataset.t); }), t);
      s = j >= 0 ? spans[j] : null;
    }
    if (s === said) return;
    if (said) said.classList.remove('said');
    said = s;
    if (!s) return;
    s.classList.add('said');
    var r = s.getBoundingClientRect();
    var bottom = pinned() ? window.innerHeight : right.getBoundingClientRect().bottom;
    if (r.bottom > bottom) { margin(s); s.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
  }
  v.addEventListener('timeupdate', follow);
  v.addEventListener('seeked', follow);
})();
"""


NOTE_STYLE = """
:root { --note: #fff3c4; --pop: #fff; --popbd: #c9ced6; }
@media (prefers-color-scheme: dark) { :root { --note: #4a4020; --pop: #23262b; --popbd: #3a3f47; } }
.note { background: var(--note); border-radius: 3px; }
.nw { position: relative; display: inline; }
.ni { font: inherit; font-size: 0.85em; line-height: 1; color: var(--link); background: none;
  border: 0; padding: 0 2px; margin-left: 2px; cursor: pointer; vertical-align: baseline; }
.np { display: none; position: absolute; left: 0; top: calc(100% - 2px); z-index: 5; width: max-content;
  max-width: min(360px, calc(100vw - 32px)); padding: 8px 12px; background: var(--pop); color: var(--fg);
  border: 1px solid var(--popbd); border-radius: 8px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
  font-size: 0.9rem; line-height: 1.45; cursor: auto; text-decoration: none; text-align: left; }
.nw.open .np { display: block; }
@media (hover: hover) { .nw:hover .np { display: block; } }
.np a { color: var(--link); text-decoration: underline; }
.np code { font-family: ui-monospace, Menlo, monospace; font-size: 0.9em; }
.np-p, .np-ul { display: block; }
.np-p + .np-p, .np-p + .np-ul, .np-ul + .np-p { margin-top: 0.5em; }
.np-li { display: list-item; list-style: disc; margin-left: 1.2em; }
"""

NOTE_SCRIPT = """
(function () {
  // Click or tap the ⓘ to open its note, Escape or a click elsewhere to
  // close it. Capture phase, so a click here never reaches the sentence's
  // click-to-play. The popup is kept inside the window.
  function place(nw) {
    var p = nw.querySelector('.np');
    p.style.left = '0px';
    var r = p.getBoundingClientRect(), shift = 0, w = document.documentElement.clientWidth;
    if (r.right > w - 8) shift = w - 8 - r.right;
    if (r.left + shift < 8) shift = 8 - r.left;
    p.style.left = shift + 'px';
  }
  function close(except) {
    document.querySelectorAll('.nw.open').forEach(function (n) {
      if (n === except) return;
      n.classList.remove('open');
      n.querySelector('.ni').setAttribute('aria-expanded', 'false');
    });
  }
  document.addEventListener('click', function (e) {
    var nw = e.target.closest('.nw');
    close(nw);
    if (!nw) return;
    e.stopPropagation();
    if (e.target.closest('.np')) return;
    var on = !nw.classList.contains('open');
    nw.classList.toggle('open', on);
    nw.querySelector('.ni').setAttribute('aria-expanded', on ? 'true' : 'false');
    if (on) place(nw);
  }, true);
  document.addEventListener('mouseover', function (e) {
    var nw = e.target.closest('.nw');
    if (nw) place(nw);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(null); });
})();
"""


def placed_notes(db, source, snap):
    # The source's notes, each marked placed when the snapshot still has its
    # words at its ids (word_key compares them); a stale note is never put
    # on other words. Shared by GET /notes and the HTML export. Provisional
    # notes are proposals Jon hasn't accepted yet.
    at = {w[0]: i for i, w in enumerate(snap["words"])} if snap else {}
    out = []
    for id_, a, b, words, md, created, updated, prov in db.execute(
            "SELECT id, w_from, w_to, words, markdown, created_at, updated_at, provisional FROM notes "
            "WHERE source = ? ORDER BY w_from, id", (source,)):
        placed = False
        if a in at and b in at and at[a] <= at[b]:
            have = [word_key(w[1]) for w in snap["words"][at[a]:at[b] + 1]]
            placed = have == [word_key(w) for w in words.split()]
        out.append({"id": id_, "from": a, "to": b, "words": words, "markdown": md, "created": created,
                    "updated": updated, "placed": placed, "provisional": bool(prov)})
    return out


def note_html(md):
    # A note's Markdown as HTML for the export, built in so the page needs
    # no package: paragraphs, bullets, links, **bold**, *italic*, `code`.
    # Everything is escaped first, so a note can't inject markup, and only
    # http(s) URLs become links. Block parts are spans (not p / ul), since
    # the popup sits inside the transcript's <p>, where those would end it.
    esc = html_lib.escape
    stash = []

    def keep(h):
        stash.append(h)
        return f"\x00{len(stash) - 1}\x01"

    def link(url, label):
        return keep(f'<a href="{url}" target="_blank" rel="noopener">{label}</a>')

    def inline(t):
        t = re.sub(r"`([^`]+)`", lambda m: keep(f"<code>{m.group(1)}</code>"), esc(t))
        t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", lambda m: link(m.group(2), m.group(1)), t)

        def bare(m):
            url = m.group(0).rstrip(".,;:!?")
            return link(url, url) + m.group(0)[len(url):]
        t = re.sub(r"https?://(?:(?!&lt;|&gt;|&quot;)\S)+", bare, t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(?!\s)(.+?)(?<!\s)\*", r"<em>\1</em>", t)
        return re.sub(r"\x00(\d+)\x01", lambda m: stash[int(m.group(1))], t)

    out = []
    for block in re.split(r"\n\s*\n", md.replace("\r\n", "\n")):
        para, items = [], []

        def flush():
            if para:
                out.append('<span class="np-p">' + "<br>".join(inline(x) for x in para) + "</span>")
                para.clear()
            if items:
                out.append('<span class="np-ul" role="list">' + "".join(
                    f'<span class="np-li" role="listitem">{inline(x)}</span>' for x in items) + "</span>")
                items.clear()
        for line in block.split("\n"):
            m = re.match(r"\s*[-*] +(.*\S)\s*$", line)
            if m:
                if para:
                    flush()
                items.append(m.group(1))
            elif line.strip():
                if items:
                    flush()
                para.append(line.strip())
        flush()
    return "".join(out)


def transcript_html(source, snap):
    # A version of the final transcript as one standalone HTML page, for a
    # static server (final-transcript-html-export): the video on the left
    # and the transcript on the right, each scrolling on its own, with a
    # splitter between them (drag, or arrow keys; its place is remembered in
    # the viewer's browser). Each turn shows its speaker in their colour
    # and its time, which seeks the video there and plays. On a narrow
    # screen the two stack, the video on top. The video is the final video
    # the version is timed by, by file name (src relative), expected beside
    # the page. Everything inline; nothing fetched from elsewhere. Turns
    # rebuilt from the version's words, as the Final transcript page shows
    # them.
    # The title, date and summary come from the project's metadata (Settings
    # › Metadata); without a title, the source's name.
    esc = html_lib.escape
    base = final_name(source)
    meta = project_meta(read_project(studio_paths.PROJECT))
    title = meta["title"] or os.path.splitext(source)[0]
    video = f"{base}-v{snap['video']}.mp4"
    lead = (f"<p class=\"date\">{esc(meta['date'])}</p>" if meta["date"] else "") \
        + "".join(f"<p class=\"summary\">{esc(p.strip())}</p>"
                  for p in re.split(r"\n\s*\n", meta["summary"]) if p.strip())
    # Each turn's words (grouped as turns_of groups them: a new turn at each
    # change of speaker), split into sentences at . ? or ! (closing quotes
    # and brackets allowed after), each a span stamped with its first word's
    # time: the page marks the one being spoken, and a click plays from it.
    # Notes (placed ones only): their words marked, and an ⓘ with the note
    # after the last. A note can run over sentences; each sentence span
    # marks its own part, so its text reads as before.
    # The export is for publishing, so a provisional note never reaches it.
    notes = [n for n in placed_notes(sqlite3.connect(DB), source, snap)
             if n["placed"] and not n["provisional"]]
    ids = [w[0] for w in snap["words"]]
    pos = {wid: k for k, wid in enumerate(ids)}
    covered, ends = set(), {}
    for n in notes:
        covered.update(ids[pos[n["from"]]:pos[n["to"]] + 1])
        ends.setdefault(n["to"], []).append(n)

    def mark(wid, w):
        text = esc(w) + "".join(
            '<span class="nw"><button type="button" class="ni" aria-label="Note" aria-expanded="false">'
            f'\u24d8</button><span class="np" role="note">{note_html(n["markdown"])}</span></span>'
            for n in ends.get(wid, []))
        return text

    def sentence_html(s):
        parts, run = [], None
        for w, at, wid in s:
            piece = mark(wid, w)
            if wid in covered:
                run = (run or []) + [piece]
                continue
            if run:
                parts.append('<span class="note">' + " ".join(run) + "</span>")
                run = None
            parts.append(piece)
        if run:
            parts.append('<span class="note">' + " ".join(run) + "</span>")
        return " ".join(parts)

    groups = []
    for wid, w, spk, how, unsure, at in snap["words"]:
        if not groups or groups[-1][0] != spk:
            groups.append((spk, []))
        groups[-1][1].append((w, at, wid))
    rows = []
    for t, (_, ws) in zip(turns_of(snap["words"], snap["names"], snap["colors"]), groups):
        color = t["color"] if re.fullmatch(r"#[0-9a-fA-F]{3,8}", t["color"] or "") else "inherit"
        sentences, cur = [], []
        for w, at, wid in ws:
            cur.append((w, at, wid))
            if re.search(r"[.?!][\"'”’)\]]*$", w):
                sentences.append(cur)
                cur = []
        if cur:
            sentences.append(cur)
        body = " ".join(f'<span class="s" data-t="{s[0][1]:.2f}">{sentence_html(s) if notes else esc(" ".join(w for w, _, _ in s))}</span>'
                        for s in sentences)
        rows.append(
            f'<div class="turn"><a class="t" href="#t={t["at"]:.2f}" data-t="{t["at"]:.2f}">{clock(t["at"])}</a>'
            f'<div><span class="who" style="color:{color}">{esc(t["name"])}</span>'
            + ('<span class="unsure" title="The voice match was unsure for most of this turn"> (?)</span>'
               if t["unsure"] else "")
            + f'<p>{body}</p></div></div>')
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title><style>{TRANSCRIPT_HTML_STYLE}{NOTE_STYLE if notes else ''}</style></head>"
        f"<body><div class=\"app\" data-show-transcript=\"{'1' if meta['showTranscript'] else '0'}\">"
        "<section class=\"left\">"
        "<button class=\"toggle\" type=\"button\" aria-pressed=\"false\">Hide transcript</button>"
        f"<h1>{esc(title)}</h1>{lead}"
        f"<video id=\"video\" controls preload=\"metadata\" src=\"{esc(urllib.parse.quote(video))}\"></video>"
        "</section><div class=\"split\" role=\"separator\" aria-orientation=\"vertical\" "
        "aria-label=\"Resize the video and transcript\" aria-valuemin=\"20\" aria-valuemax=\"80\" "
        "aria-valuenow=\"45\" tabindex=\"0\"></div><section class=\"right\">\n" + "\n".join(rows)
        + f"\n</section></div><script>{TRANSCRIPT_HTML_SCRIPT}</script>"
        + (f"<script>{NOTE_SCRIPT}</script>" if notes else "") + "</body></html>\n")


def transcript_summary(source, snap, latest_video):
    base = final_name(source)
    names = snap["names"]
    video = os.path.join(finals_dir(), f"{base}-v{snap['video']}.mp4")
    return {"n": snap["n"], "madeAt": snap["madeAt"], "video": snap["video"],
            "videoUrl": final_url(f"{base}-v{snap['video']}.mp4") if os.path.isfile(video) else None,
            "turns": len(snap["turns"]), "unsure": sum(1 for t in snap["turns"] if t["unsure"]),
            "samples": [{"name": names.get(k, k), "secs": v} for k, v in snap["samples"].items()],
            "md": final_url(f"{base}-transcript-v{snap['n']}.md"),
            "html": f"http://127.0.0.1:{PORT}/podcast/final-transcript/html?source="
                    f"{urllib.parse.quote(source)}&n={snap['n']}",
            "timedByLatest": latest_video is not None and snap["video"] == latest_video}


def compare_transcripts(a, b):
    # What changed from version a to version b, by word id (both snapshots
    # hold every kept word): stretches whose speaker changed, in b's order,
    # each with its time in b's video; words per speaker in each; and words
    # in only one of them (the edit changed in between), counted.
    sa = {w[0]: w for w in a["words"]}
    names = {**a["names"], **b["names"]}
    name = lambda s: names.get(str(s), "Unknown") if s is not None else "Unknown"
    changes, run = [], None
    for k, (wid, w, spk, how, unsure, at) in enumerate(b["words"]):
        old = sa.get(wid)
        if old is not None and old[2] != spk:
            if run and run["_last"] == k - 1 and run["fromId"] == old[2] and run["toId"] == spk:
                run["words"].append(w)
            else:
                run = {"at": at, "fromId": old[2], "toId": spk, "from": name(old[2]), "to": name(spk),
                       "words": [w], "unsure": unsure}
                changes.append(run)
            run["_last"] = k
        else:
            run = None
    count = lambda snap: collections.Counter(name(w[2]) for w in snap["words"])
    ca, cb = count(a), count(b)
    totals = [{"name": n, "a": ca.get(n, 0), "b": cb.get(n, 0)} for n in sorted(set(ca) | set(cb))]
    ids_a, ids_b = set(sa), {w[0] for w in b["words"]}
    return {"changes": [{"at": c["at"], "from": c["from"], "to": c["to"], "text": " ".join(c["words"]),
                         "words": len(c["words"])} for c in changes],
            "changedWords": sum(len(c["words"]) for c in changes), "totals": totals,
            "onlyA": len(ids_a - ids_b), "onlyB": len(ids_b - ids_a)}


def make_final_transcript(source):
    # The background half of POST /podcast/final-transcript: match voices
    # (match_speakers, as Match voices did: your samples stay, the other
    # words get the speaker whose voice they match), then a new version.
    t_start = time.time()
    match_speakers(source)
    try:
        db = sqlite3.connect(DB)
        with TEXT_LOCK:
            num = save_transcript_version(db, source)
        snap = dict(versions(source, "transcript")).get(num) if num else None
        print(f"final transcript source={source} v={num} turns={len(snap['turns']) if snap else 0} "
              f"unsure={sum(1 for t in snap['turns'] if t['unsure']) if snap else 0} "
              f"secs={time.time() - t_start:.1f}", flush=True)
        push({"finalTranscript": source, "finished": True, "n": num})
    except Exception as e:
        print(f"final transcript failed source={source} error={e}", flush=True)
        push({"finalTranscript": source, "error": str(e)})


def delete_final(source, kind, n):
    # One version, its files; a deleted video's transcripts keep their text
    # but lose their player. Returns whether it existed.
    base = final_name(source) + ("-transcript" if kind == "transcript" else "")
    exts = (".md", ".json") if kind == "transcript" else (".mp4", ".json")
    found = False
    for ext in exts:
        try:
            os.remove(os.path.join(finals_dir(), f"{base}-v{n}{ext}"))
            found = True
        except OSError:
            pass
    if kind == "video":
        link_latest_video(source)
    print(f"final deleted source={source} kind={kind} v={n} found={found}", flush=True)
    return found


CHECK_WINDOW = 1.5  # seconds of the edit either side of a join where a mismatch counts against it
RECHECK = 12.0  # seconds of the edit around a suspect join that whisper hears again on its own


def heard_words(path):
    # whisper's words in a transcript.json-style file: [key, start, end].
    words = []
    for seg in json.load(open(path))["transcription"]:
        for tok in seg.get("tokens", []):
            text = tok["text"]
            if text.startswith("[_") or not text.strip():
                continue
            t0, t1 = tok["offsets"]["from"] / 1000, tok["offsets"]["to"] / 1000
            if words and not text.startswith(" "):
                words[-1][0] += text
                words[-1][2] = t1
            else:
                words.append([text.strip(), t0, t1])
    return [[word_key(w), a, b] for w, a, b in words if word_key(w)]


def check_text(source, lo, hi):
    # The round-trip check (POST /transcripts/check): render the kept audio
    # between source seconds lo and hi, transcribe it, and diff what is heard
    # against the kept words. A join is suspect when a mismatch falls within
    # CHECK_WINDOW of it; mismatches far from any join are whisper's own
    # noise, counted so a verdict can be weighed. Verdicts go to check.json
    # beside the transcript, keyed by the first word id of the cut after each
    # join, where the cut list (text_cuts) picks them up.
    import difflib
    t_start = time.time()
    folder = transcript_folder(source)
    try:
        db = sqlite3.connect(DB)
        duration, prompt = db.execute("SELECT duration_s, prompt FROM transcripts WHERE source = ?",
                                      (source,)).fetchone()
        hi = min(hi or duration, duration)
        with TEXT_LOCK:
            cuts = text_cuts(db, source, duration)
            kept = db.execute("SELECT t0, coalesce(shown, text) FROM words WHERE source = ? AND kept = 1 "
                              "AND t0 >= ? AND t0 < ? ORDER BY i", (source, lo, hi)).fetchall()
        pieces = [(max(k["src0"], lo), min(k["src1"], hi)) for k in cuts["kept"] if k["src1"] > lo and k["src0"] < hi]
        pieces = [p for p in pieces if p[1] - p[0] > 0.02]
        after = {s["t0"]: s for s in cuts["spans"]}
        joins, out, starts = [], 0.0, []
        for n, (a, b) in enumerate(pieces):
            starts.append((a, b, out))
            out += b - a
            if n + 1 < len(pieces) and round(b, 3) in after:
                joins.append({"at": out, "span": after[round(b, 3)]})

        def edited(t):  # a source second on the edited timeline
            for a, b, o in starts:
                if t < b:
                    return o + max(0.0, t - a)
            return out

        wav = os.path.join(folder, "check.wav")
        fade = podcast_settings()["fadeMs"] / 1000
        parts = [f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS"
                 + (f",afade=t=in:d={fade},afade=t=out:st={max(b - a - fade, 0)}:d={fade}" if fade else "")
                 + f"[p{n}]" for n, (a, b) in enumerate(pieces)]
        graph = ";".join(parts) + ";" + "".join(f"[p{n}]" for n in range(len(pieces))) + \
            f"concat=n={len(pieces)}:v=0:a=1[out]"
        # The graph is read from a file (ffmpeg's "-/option file" form; this
        # ffmpeg has no -filter_complex_script), since a long edit's graph
        # outgrows a command line.
        script = os.path.join(folder, "check-graph.txt")
        open(script, "w").write(graph)
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", os.path.join(folder, "audio-16k.wav"),
                            "-/filter_complex", script, "-map", "[out]", "-c:a", "pcm_s16le", wav],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("render: " + (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        # whisper's segment lines are the progress, pushed every 2 s as
        # {"checking": source, done: false, done_s, duration_s}, as a
        # transcription's are: a full check takes minutes.
        proc = subprocess.Popen(["whisper-cli", "-m", transcribe_model(), "-f", wav, "-l", "en", "-t", "8", "-ojf",
                                 "-of", os.path.join(folder, "check-heard"), "--prompt",
                                 f"{DISFLUENT_PROMPT} {prompt or ''}".strip()],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        last_push = 0
        for line in proc.stdout:
            m = SEGMENT_LINE.match(line)
            if m and time.time() - last_push > 2:
                last_push = time.time()
                push({"checking": source, "done": False, "duration_s": out,
                      "done_s": int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))})
        if proc.wait():
            raise RuntimeError(f"whisper-cli exited {proc.returncode}")
        heard = heard_words(os.path.join(folder, "check-heard.json"))
        expected = [[word_key(w), edited(t), w] for t, w in kept if word_key(w)]
        ops = [o for o in difflib.SequenceMatcher(None, [e[0] for e in expected], [h[0] for h in heard],
                                                  autojunk=False).get_opcodes() if o[0] != "equal"]

        def where(op):
            _, i1, i2, j1, j2 = op
            if i1 < len(expected):
                return expected[i1][1], expected[max(i1, i2 - 1)][1]
            return heard[j1][1], heard[max(j1, j2 - 1)][1]

        near = set()
        verdicts = {}
        for j in joins:
            bad = [o for o in ops if where(o)[0] - CHECK_WINDOW <= j["at"] <= where(o)[1] + CHECK_WINDOW]
            near.update(map(id, bad))
            e_ctx = [e[2] for e in expected if abs(e[1] - j["at"]) <= CHECK_WINDOW]
            h_ctx = [h[0] for h in heard if abs(h[1] - j["at"]) <= CHECK_WINDOW]
            verdicts[str(j["span"]["ids"][0])] = {"ok": not bad, "kept": " ".join(e_ctx), "heard": " ".join(h_ctx)}
        noise = sum(1 for o in ops if id(o) not in near)
        # A second opinion on each suspect join: whisper again on just the
        # RECHECK seconds around it. Over a whole hour whisper hears some
        # phrases differently from one run to the next ("and this is before"
        # vs "that was before" for the same audio, 2026-11-13), so a join
        # stays suspect only if the short listen disagrees with the text too.
        for j in joins:
            v = verdicts[str(j["span"]["ids"][0])]
            if v["ok"]:
                continue
            c0 = max(0.0, j["at"] - RECHECK / 2)
            clip = os.path.join(folder, "recheck.wav")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{c0:.3f}", "-t", str(RECHECK),
                            "-i", wav, "-c:a", "pcm_s16le", clip], capture_output=True)
            r = subprocess.run(["whisper-cli", "-m", transcribe_model(), "-f", clip, "-l", "en", "-t", "8",
                                "-ojf", "-of", os.path.join(folder, "recheck"), "--prompt", DISFLUENT_PROMPT],
                               capture_output=True, text=True)
            if r.returncode:
                continue
            near_h = [h for h in heard_words(os.path.join(folder, "recheck.json"))
                      if 1.0 <= h[1] <= RECHECK - 1.0]
            if not near_h:
                continue
            exp = [e for e in expected if c0 + near_h[0][1] - 0.5 <= e[1] <= c0 + near_h[-1][2] + 0.5]
            again = [o for o in difflib.SequenceMatcher(None, [e[0] for e in exp], [h[0] for h in near_h],
                                                        autojunk=False).get_opcodes() if o[0] != "equal"]
            v["recheck"] = " ".join(h[0] for h in near_h)
            v["ok"] = not again
        suspect = sum(1 for v in verdicts.values() if not v["ok"])
        json.dump({"at": datetime_now(), "range": [lo, hi], "joins": len(joins), "suspect": suspect,
                   "noise": noise, "spans": verdicts}, open(os.path.join(folder, "check.json"), "w"))
        print(f"check source={source} range={lo:.0f}-{hi:.0f} joins={len(joins)} suspect={suspect} "
              f"noise={noise} secs={time.time() - t_start:.1f}", flush=True)
    except Exception as e:
        print(f"check failed source={source} error={e}", flush=True)
    finally:
        with TRANSCRIBE_LOCK:
            checking.pop(source, None)
        push({"checking": source, "done": True})
        notify("words")


def add_speaker(db, name):
    n = db.execute("SELECT count(*) FROM speakers").fetchone()[0]
    cur = db.execute("INSERT INTO speakers (name, color, position) VALUES (?, ?, ?)",
                     (name, SPEAKER_COLORS[n % len(SPEAKER_COLORS)], n))
    return cur.lastrowid


def choose_port():
    # Each checkout's server gets its own port, so several Studios can run at
    # once (per-checkout-server-port): an explicit argument, else STUDIO_PORT
    # (set just below, so a project switch's restart keeps its port), else
    # 8765 if free, else the next free one. The choice is published in
    # studio-port.json at the repo root (serve(), below), where the page,
    # start.sh and the scripts read it.
    if len(sys.argv) > 1:
        return int(sys.argv[1])
    if os.environ.get("STUDIO_PORT"):
        return int(os.environ["STUDIO_PORT"])
    for p in range(8765, 8800):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # The server binds with SO_REUSEADDR too (HTTPServer's
            # allow_reuse_address); on macOS it still refuses a port another
            # socket is listening on.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("serve_media.py: no free port in 8765-8799")


PORT = choose_port()
os.environ["STUDIO_PORT"] = str(PORT)  # for restart() and every script it launches
# STUDIO_PORT_FILE moves it, so a test server doesn't overwrite the live one.
PORT_FILE = os.environ.get("STUDIO_PORT_FILE") or os.path.join(HERE, "studio-port.json")
# A URL saved in a database before this checkout's port could vary (takes.url,
# written by register.py and overlay.py) names the port it was made on; /query
# rewrites it to this server's port on the way out, so old projects work on
# any port without a migration.
LOCAL_URL = re.compile(r"^http://127\.0\.0\.1:\d+/")


def serve():
    # Bind first, then publish the port: the file only ever names a port
    # this server holds.
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    with open(PORT_FILE, "w") as f:
        json.dump({"port": PORT, "pid": os.getpid(), "root": HERE}, f)
    print(f"serving on port {PORT} (studio-port.json)", flush=True)
    httpd.serve_forever()
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
        # CORS only for an allowed origin, echoed back, never "*"
        # (server-origin-host-gate); a refused request gets none.
        origin = self.headers.get("Origin")
        if origin and origin_allowed(origin) and not getattr(self, "refused", False):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        # ?download=1 on a served file: saved, not shown (the Final video
        # page's Download video, the final transcript's Download Markdown).
        parsed = urllib.parse.urlparse(self.path)
        if "download=1" in parsed.query.split("&"):
            name = os.path.basename(urllib.parse.unquote(parsed.path))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        super().end_headers()

    # The app runs on Bram's origin, so its POSTs here are cross-origin and
    # preflighted. Allow whatever headers it asks for: XMLUI adds its own
    # x-ue-client-tx-id to every request, not just Content-Type.
    def do_OPTIONS(self):
        if self.refuse_foreign():
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         self.headers.get("Access-Control-Request-Headers", "Content-Type"))
        self.end_headers()

    # With no project yet (NO_PROJECT), only the routes the no-project page
    # needs answer; everything else gets one clear refusal instead of
    # "no such table" from the repo's bare studio.db. /projects/new is the
    # way out: it creates the first project and restarts the server into it.
    NO_PROJECT_GET = ("/projects", "/sources", "/devices", "/dictation/status",
                      "/dictation/engine", "/narrate/status", "/events", "/trace",
                      "/identity")
    NO_PROJECT_POST = ("/projects/new", "/page/health", "/audition/log", "/trace")

    # The gate every request passes first (server-origin-host-gate): a page
    # from a non-local origin, or a Host that isn't loopback on this
    # server's own port, gets a bare 403 before any handler runs. The
    # connection closes, so an unread POST body can't be taken for the next
    # request. Logged, since a refusal the user didn't expect is a bug report.
    def refuse_foreign(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if (origin is None or origin_allowed(origin)) and host_allowed(host, PORT):
            return False
        self.refused = True
        self.close_connection = True
        print(f"refused method={self.command} path={self.path.split('?')[0]} "
              f"origin={origin!r} host={host!r}", flush=True)
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    # The parent class serves files on HEAD too, so it passes the gate.
    def do_HEAD(self):
        if self.refuse_foreign():
            return
        super().do_HEAD()

    def no_project_refused(self, allowed):
        if NO_PROJECT and self.path.split("?")[0] not in allowed:
            self.send_json(409, {"error": "no project yet; create one with New project"})
            return True
        return False

    def do_GET(self):
        if self.refuse_foreign():
            return
        if self.no_project_refused(self.NO_PROJECT_GET):
            return
        if self.path == "/trace":
            return self.send_json(200, {"on": trace_on()})
        if self.path == "/settings":
            return self.send_json(200, {**read_settings(), "podcast": podcast_settings(),
                                        "podcastDefaults": {**{k: v[0] for k, v in PODCAST_SETTINGS.items()}, **PODCAST_WORD_SETTINGS}})
        if self.path == "/identity":
            # Whose server is this? The page compares bramPort (the Bram
            # instance recorded in this checkout's .bram/.bram-port.json)
            # with its own origin port: same checkout, same Bram. An orphan
            # server from another checkout answers with that checkout's
            # Bram port, and the page treats it as down rather than show an
            # editor backed by the wrong repo (probe-checks-server-identity).
            try:
                with open(os.path.join(HERE, ".bram", ".bram-port.json")) as f:
                    bram_port = json.load(f).get("port")
            except (OSError, ValueError):
                bram_port = None
            return self.send_json(200, {"root": HERE, "bramPort": bram_port})
        if self.path == "/devices":
            return self.send_json(200, [{"name": n} for n in voicetest.devices()])
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
        if self.path.startswith("/transcripts/status"):
            return self.transcript_status()
        if self.path.startswith("/transcripts/text"):
            return self.transcript_text()
        if self.path.startswith("/transcripts/cuts"):
            return self.transcript_cuts()
        if self.path.startswith("/transcripts/paras"):
            return self.transcript_paras()
        if self.path.startswith("/speakers/sample-audio"):
            return self.sample_audio_route()
        if self.path.startswith("/speakers/samples"):
            source = self.query_source()
            db = sqlite3.connect(DB)
            return self.send_json(200, sample_list(db, source))
        if self.path.startswith("/speakers/stats"):
            return self.speaker_stats()
        if self.path.startswith("/transcripts/play"):
            return self.play_para()
        if self.path.startswith("/transcripts/poster"):
            return self.poster_para()
        if self.path.startswith("/exports/poster"):
            return self.poster_export()
        if self.path.startswith("/transcripts/marks"):
            return self.para_marks_route()
        if self.path.startswith("/screencast/final"):
            return self.send_json(200, screencast_finals(sqlite3.connect(DB)))
        if self.path.startswith("/final/at"):
            return self.final_at_route()
        if self.path.split("?")[0] == "/notes":
            return self.notes_route()
        if self.path.startswith("/podcast/final-transcript"):
            return self.final_transcript_route()
        if self.path.startswith("/podcast/final"):
            return self.final_route()
        super().do_GET()

    def stream_events(self):
        # Server-sent events for the page's <EventSource>: data: {"changed":
        # "takes"|"sources"|"exports"|"projects"} as watch_changes() sees them,
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
        if self.refuse_foreign():
            return
        if self.no_project_refused(self.NO_PROJECT_POST):
            return
        if self.path == "/trace":
            return self.set_trace()
        if urllib.parse.urlparse(self.path).path == "/query":  # ?p=<project>: the pages' cache key only
            return self.query(DB)
        if self.path == "/query/shared":
            return self.query(SHARED_DB, shared=True)
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
        if self.path == "/projects/delete-open":
            return self.delete_open_project()
        if self.path == "/projects/meta":
            # POST /projects/meta {title, date, summary}: the open project's
            # metadata, saved in its project.json (project_meta).
            body = self.json_body()
            if any(not isinstance(body.get(k, ""), str) for k in META_FIELDS) \
                    or not isinstance(body.get("showTranscript", False), bool):
                return self.send_json(400, {"error": "expected strings title, date, summary and a boolean showTranscript"})
            info = read_project(studio_paths.PROJECT)
            if info is None:
                return self.send_json(404, {"error": "no open project"})
            info["meta"] = {**{k: body.get(k, "").strip() for k in META_FIELDS},
                            "showTranscript": body.get("showTranscript", False)}
            write_project(studio_paths.PROJECT, info)
            print("project meta " + " ".join(f"{k}={len(info['meta'][k])}" for k in META_FIELDS)
                  + f" showTranscript={info['meta']['showTranscript']}", flush=True)
            return self.send_json(200, info["meta"])
        if self.path == "/projects/rename":
            return self.rename_project()
        if self.path == "/projects/new":
            return self.new_project()
        if self.path == "/projects/sources/add":
            return self.add_project_source()
        if self.path == "/projects/stray/delete":
            return self.delete_stray()
        if self.path == "/projects/clean":
            return self.clean_project()
        if self.path == "/settings":
            return self.save_settings()
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
        if self.path == "/audition/log":
            return self.log_audition()
        if self.path == "/undo/log":
            return self.log_undo_gate()
        if self.path == "/podcast/log":
            return self.log_podcast()
        if self.path == "/page/health":
            return self.log_page_health()
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
        if self.path in ("/notes/add", "/notes/update", "/notes/delete"):
            return self.edit_note(self.path.rsplit("/", 1)[1])
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
        if self.path == "/transcripts/start":
            return self.start_transcript()
        if self.path in ("/speakers/add", "/speakers/rename", "/speakers/delete"):
            return self.edit_speaker(self.path.rsplit("/", 1)[1])
        if self.path == "/segments/speaker":
            return self.label_segments()
        if self.path in ("/transcripts/edit", "/transcripts/undo", "/transcripts/redo", "/transcripts/restore",
                         "/transcripts/elide", "/transcripts/replace"):
            return self.edit_text(self.path.rsplit("/", 1)[1])
        if self.path == "/transcripts/check":
            return self.start_check()
        if self.path == "/words/speaker":
            return self.tag_words()
        if self.path == "/speakers/propagate":
            return self.start_matching()
        if self.path == "/podcast/final":
            return self.start_final()
        if self.path == "/podcast/final-transcript":
            return self.start_final_transcript()
        if self.path in ("/podcast/final/delete", "/podcast/final-transcript/delete"):
            return self.delete_final_route()
        self.send_json(404, {"error": "not found"})

    def query_source(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("source", [""])[0]

    def transcript_text(self):
        # GET /transcripts/text?source=: the kept words as editable text.
        source = self.query_source()
        db = sqlite3.connect(DB)
        with TEXT_LOCK:
            if not ensure_words(db, source):
                return self.send_json(404, {"error": f"no transcript of {source!r}"})
            self.send_json(200, text_summary(db, source))

    def transcript_paras(self):
        # GET /transcripts/paras?source=: the paragraphs to edit (paragraphs).
        source = self.query_source()
        db = sqlite3.connect(DB)
        with TEXT_LOCK:
            if not ensure_words(db, source):
                return self.send_json(404, {"error": f"no transcript of {source!r}"})
            paras = paragraphs(db, source, with_words="words=1" in self.path)
            # A paragraph holding a join the last check flagged (check.json
            # keys are the first word of the cut after each join).
            try:
                flagged = [int(k) for k, v in json.load(open(os.path.join(transcript_folder(source), "check.json")))[
                    "spans"].items() if not v["ok"]]
            except (OSError, ValueError, KeyError):
                flagged = []
            marks = {r[0] for r in db.execute(
                f"SELECT para FROM words WHERE id IN ({','.join('?' * len(flagged))})", flagged)} if flagged else set()
            for p in paras:
                p["suspect"] = p["para"] in marks
            self.send_json(200, paras)

    def play_para(self):
        # GET /transcripts/play?source=&para=&v=: a paragraph's edited audio
        # (para_audio), by redirect to the rendered file, which the ordinary
        # file serving streams with byte ranges. v is only for the browser's
        # cache: a new version is a new address.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        source = query.get("source", [""])[0]
        db = sqlite3.connect(DB)
        row = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()
        try:
            para = int(query.get("para", [""])[0])
        except ValueError:
            return self.send_json(400, {"error": "expected para"})
        with TEXT_LOCK:
            if not row or not ensure_words(db, source):
                return self.send_json(404, {"error": f"no transcript of {source!r}"})
            try:
                name = para_audio(db, source, para, row[0] or 0)
            except Exception as e:
                print(f"para audio failed source={source} para={para} error={e}", flush=True)
                return self.send_json(500, {"error": str(e)})
        if not name:
            return self.send_json(404, {"error": "nothing kept in this paragraph"})
        self.send_response(302)
        self.send_header("Location", "/.preview/paras/" + urllib.parse.quote(name))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def poster_para(self):
        # GET /transcripts/poster?source=&para=&v=: a still of the paragraph's
        # clip (para_poster), by redirect, like /transcripts/play.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        source = query.get("source", [""])[0]
        db = sqlite3.connect(DB)
        row = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()
        try:
            para = int(query.get("para", [""])[0])
        except ValueError:
            return self.send_json(400, {"error": "expected para"})
        with TEXT_LOCK:
            if not row or not ensure_words(db, source):
                return self.send_json(404, {"error": f"no transcript of {source!r}"})
            try:
                name = para_poster(db, source, para, row[0] or 0)
            except Exception as e:
                print(f"para poster failed source={source} para={para} error={e}", flush=True)
                return self.send_json(500, {"error": str(e)})
        if not name:
            return self.send_json(404, {"error": "nothing kept in this paragraph"})
        self.send_response(302)
        self.send_header("Location", "/.preview/paras/" + urllib.parse.quote(name))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def para_marks_route(self):
        # GET /transcripts/marks?source=&para=: where each kept word of a
        # paragraph is in its text and its rendered audio (para_marks).
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        source = query.get("source", [""])[0]
        try:
            para = int(query.get("para", [""])[0])
        except ValueError:
            return self.send_json(400, {"error": "expected para"})
        db = sqlite3.connect(DB)
        row = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()
        with TEXT_LOCK:
            if not row or not ensure_words(db, source):
                return self.send_json(404, {"error": f"no transcript of {source!r}"})
            self.send_json(200, para_marks(db, source, para, row[0] or 0))

    def transcript_cuts(self):
        # GET /transcripts/cuts?source=: the edit list (text_cuts).
        source = self.query_source()
        db = sqlite3.connect(DB)
        row = db.execute("SELECT duration_s FROM transcripts WHERE source = ?", (source,)).fetchone()
        with TEXT_LOCK:
            if not row or not ensure_words(db, source):
                return self.send_json(404, {"error": f"no transcript of {source!r}"})
            self.send_json(200, text_cuts(db, source, row[0] or 0))

    def tag_words(self):
        # {source, from_id, to_id, speaker_id}: tag the kept words from one
        # to the other (either order) as that speaker; speaker_id null clears
        # them. A tag is yours: matching never overwrites it.
        body = self.json_body()
        source = body.get("source") or ""
        db = sqlite3.connect(DB)
        ends = [r[0] for r in db.execute("SELECT i FROM words WHERE source = ? AND id IN (?, ?)",
                                         (source, body.get("from_id"), body.get("to_id")))]
        if not ends:
            return self.send_json(404, {"error": "no such words"})
        lo, hi = min(ends), max(ends)
        spk = body.get("speaker_id")
        if spk is not None and not db.execute("SELECT 1 FROM speakers WHERE id = ?", (spk,)).fetchone():
            return self.send_json(404, {"error": f"no speaker with id {spk}"})
        # One Undo step, recording each word's speaker before and after, so
        # Undo restores exactly what was there and Redo re-applies it.
        how = "tag" if spk is not None else None
        # Tagging takes the kept words; clearing takes every word in the
        # range, cut ones too: a sample whose words were cut after it was
        # tagged is still listed (/speakers/samples) and must still clear
        # (sample-clear-cut-words).
        kept = " AND kept = 1" if spk is not None else ""
        with TEXT_LOCK:
            rows = db.execute("SELECT id, speaker_id, speaker_how, speaker_unsure FROM words "
                              f"WHERE source = ?{kept} AND i BETWEEN ? AND ? ORDER BY i",
                              (source, lo, hi)).fetchall()
            tagged = [{"id": wid, "was": [s0, h0, u0], "now": [spk, how, 0]} for wid, s0, h0, u0 in rows
                      if (s0, h0) != (spk, how)]
            if tagged:
                db.executemany("UPDATE words SET speaker_id = ?, speaker_how = ?, speaker_unsure = 0 WHERE id = ?",
                               [(spk, how, t["id"]) for t in tagged])
                db.execute("DELETE FROM transcript_edits WHERE source = ? AND undone = 1", (source,))
                db.execute("INSERT INTO transcript_edits (source, created_at, changes) VALUES (?, ?, ?)",
                           (source, datetime_now(), json.dumps({"cut": [], "restored": [], "corrected": [],
                                                                "tagged": tagged, "reason": "tag"})))
            db.commit()
        print(f"tag source={source} words={len(tagged)} speaker={spk}", flush=True)
        notify("words")
        self.send_json(200, {"tagged": len(tagged)})

    def speaker_stats(self):
        # GET /speakers/stats?source=: each speaker's tagged and matched
        # speech in this source, and whether it has enough tagged to match.
        source = self.query_source()
        db = sqlite3.connect(DB)
        need = podcast_settings()["vpMinTaggedS"]
        rows = db.execute(
            "SELECT s.id, s.name, s.color, "
            "coalesce(sum(CASE WHEN w.speaker_how = 'tag' THEN w.t1 - w.t0 END), 0), "
            "coalesce(sum(CASE WHEN w.speaker_how = 'voiceprint' THEN w.t1 - w.t0 END), 0) "
            "FROM speakers s LEFT JOIN words w ON w.speaker_id = s.id AND w.source = ? AND w.kept = 1 "
            "GROUP BY s.id ORDER BY s.position, s.id", (source,)).fetchall()
        with TRANSCRIBE_LOCK:
            busy = matching.get(source)
        self.send_json(200, {"need": need, "matching": busy, "speakers": [
            {"id": i, "name": n, "color": c, "tagged": round(t, 1), "matched": round(m, 1), "ready": t >= need}
            for i, n, c, t, m in rows]})

    def start_matching(self):
        # {source}: voiceprint.py in the background (match_speakers), one at
        # a time; progress on /events as {"matching": source, done, total}.
        body = self.json_body()
        source = body.get("source") or ""
        db = sqlite3.connect(DB)
        if not db.execute("SELECT 1 FROM words WHERE source = ? LIMIT 1", (source,)).fetchone():
            return self.send_json(404, {"error": f"no transcript of {source!r}"})
        if not (os.path.isfile(SPEECH_PYTHON) and os.path.isfile(SPEAKER_MODEL)):
            return self.send_json(409, {"error": f"the speaker model isn't installed ({SPEAKER_MODEL})"})
        with TRANSCRIBE_LOCK:
            if matching:
                return self.send_json(409, {"error": "voices are already being matched"})
            matching[source] = {"done": 0, "total": 0}
        threading.Thread(target=match_speakers, args=(source,), daemon=True).start()
        push({"matching": source, "done": 0, "total": 0})
        self.send_json(200, {"source": source, "matching": True})

    def sample_audio_route(self):
        # GET /speakers/sample-audio?source=&t0=&t1=: that stretch of the
        # source's sound alone (a sample, or a flagged part of one), cut once
        # into media/.preview/samples/ and served by redirect, so a player's
        # scrubber spans just the sample.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        source = query.get("source", [""])[0]
        try:
            t0, t1 = float(query.get("t0", [""])[0]), float(query.get("t1", [""])[0])
        except ValueError:
            return self.send_json(400, {"error": "expected t0 and t1"})
        path_in = source_paths().get(source)
        if not path_in or not 0 <= t0 < t1 or t1 - t0 > 600:
            return self.send_json(404, {"error": "no such stretch"})
        stem = re.sub(r"[^\w.-]+", "_", os.path.splitext(source)[0])
        name = f"{stem}-{int(t0 * 1000)}-{int(t1 * 1000)}.m4a"
        folder = os.path.join(ROOT, ".preview", "samples")
        out = os.path.join(folder, name)
        if not os.path.isfile(out):
            os.makedirs(folder, exist_ok=True)
            r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}",
                                "-i", path_in, "-vn", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                                out + ".tmp.m4a"], capture_output=True, text=True)
            if r.returncode:
                return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1]})
            os.replace(out + ".tmp.m4a", out)
        self.send_response(302)
        self.send_header("Location", "/.preview/samples/" + urllib.parse.quote(name))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def start_final(self):
        # {source}: the final video in the background (make_final), one per
        # source at a time; progress on /events as {"final": source, done, total}.
        source = self.json_body().get("source") or ""
        db = sqlite3.connect(DB)
        if not db.execute("SELECT 1 FROM words WHERE source = ? AND kept = 1 LIMIT 1", (source,)).fetchone():
            return self.send_json(404, {"error": f"nothing kept in {source!r}"})
        with FINAL_LOCK:
            if source in finals:
                return self.send_json(409, {"error": "the final video is already being made"})
            finals[source] = {"done": 0, "total": 0}
        threading.Thread(target=make_final, args=(source,), daemon=True).start()
        push({"final": source, "done": 0, "total": 0})
        self.send_json(200, {"source": source, "making": True})

    def poster_export(self):
        # GET /exports/poster?url=<an exported video's url>: a still of the
        # video, its first frame, for Final video's poster: the player has
        # the video's size the moment it mounts, not a short box until the
        # file's metadata loads (final-video-steady). Only files under
        # media/exports/. Cached in media/.preview/posters/ by name, size and
        # mtime, so a remade or leveled file gets a new still; by redirect.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        rel = urllib.parse.unquote(urllib.parse.urlparse(query.get("url", [""])[0]).path).lstrip("/")
        exports = os.path.realpath(os.path.join(ROOT, "exports"))
        path = os.path.realpath(os.path.join(ROOT, rel))
        if not path.startswith(exports + os.sep) or not os.path.isfile(path):
            return self.send_json(404, {"error": "no such exported video"})
        st = os.stat(path)
        base = re.sub(r"[^\w.-]+", "_", os.path.relpath(path, exports))
        jpg = f"{base}-{st.st_size}-{int(st.st_mtime)}.jpg"
        posters = os.path.join(ROOT, ".preview", "posters")
        out = os.path.join(posters, jpg)
        if not os.path.isfile(out):
            os.makedirs(posters, exist_ok=True)
            r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path,
                                "-frames:v", "1", "-q:v", "4", out + ".tmp.jpg"], capture_output=True, text=True)
            if r.returncode:
                err = (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1]
                print(f"export poster failed file={rel} error={err}", flush=True)
                return self.send_json(500, {"error": err})
            os.replace(out + ".tmp.jpg", out)
            for old in os.listdir(posters):
                if old.startswith(base + "-") and old != jpg:
                    os.remove(os.path.join(posters, old))
        self.send_response(302)
        self.send_header("Location", "/.preview/posters/" + urllib.parse.quote(jpg))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def final_route(self):
        # GET /podcast/final?source=: final_state, less the clip list.
        source = self.query_source()
        db = sqlite3.connect(DB)
        with TEXT_LOCK:
            if not ensure_words(db, source):
                # A new project, not yet transcribed: nothing made, not an error.
                return self.send_json(200, {"exists": False, "versions": [], "making": None, "noTranscript": True})
            state = final_state(db, source)
        self.send_json(200, state)

    def final_at_route(self):
        # GET /final/at?source=&para=N: the newest final video's version and
        # the time in it where paragraph N starts (final_at).
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        source = query.get("source", [""])[0]
        try:
            para = int(query.get("para", [""])[0])
        except ValueError:
            return self.send_json(400, {"error": "expected para"})
        db = sqlite3.connect(DB)
        with TEXT_LOCK:
            if not ensure_words(db, source):
                return self.send_json(200, {"n": None, "at": None})
            self.send_json(200, final_at(db, source, para))

    def final_transcript_route(self):
        # GET /podcast/final-transcript?source=&n=: the transcript versions
        # (newest first) and one of them (n, else the newest) with its turns.
        # needVideo when there's no final video to time it by.
        # GET /podcast/final-transcript/compare?source=&a=&b=: what changed
        # from version a to b (compare_transcripts).
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        source = query.get("source", [""])[0]
        db = sqlite3.connect(DB)
        with TEXT_LOCK:
            if not ensure_words(db, source):
                return self.send_json(200, {"needVideo": True, "noTranscript": True, "versions": [], "version": None,
                                            "turns": [], "remakeWhy": [], "upToDate": False, "editChanged": False,
                                            "matching": None})
            state = final_state(db, source)
        snaps = dict(versions(source, "transcript"))
        if self.path.startswith("/podcast/final-transcript/html"):
            # GET /podcast/final-transcript/html?source=&n=: version n as a
            # standalone HTML page, saved (transcript_html).
            try:
                n = int(query.get("n", [""])[0])
            except ValueError:
                return self.send_json(400, {"error": "expected n"})
            if n not in snaps:
                return self.send_json(404, {"error": "no such version"})
            # ASCII, every other character as an entity: it reads the same
            # whatever charset a host labels it with (Apache's directory
            # listings say ISO-8859-1).
            body = transcript_html(source, snaps[n]).encode("ascii", "xmlcharrefreplace")
            name = f"{final_name(source)}-transcript-v{n}.html"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"final transcript html source={source} n={n} bytes={len(body)}", flush=True)
            return
        if self.path.startswith("/podcast/final-transcript/compare"):
            try:
                a, b = int(query.get("a", [""])[0]), int(query.get("b", [""])[0])
            except ValueError:
                return self.send_json(400, {"error": "expected a and b"})
            if a not in snaps or b not in snaps:
                return self.send_json(404, {"error": "no such version"})
            base = final_name(source)
            video = os.path.join(finals_dir(), f"{base}-v{snaps[b]['video']}.mp4")
            return self.send_json(200, {**compare_transcripts(snaps[a], snaps[b]), "a": a, "b": b,
                                        "videoUrl": final_url(f"{base}-v{snaps[b]['video']}.mp4")
                                        if os.path.isfile(video) else None})
        latest = state["n"] if state["exists"] else None
        listed = [transcript_summary(source, snaps[n], latest) for n in sorted(snaps, reverse=True)]
        try:
            pick = int(query.get("n", [""])[0])
        except ValueError:
            pick = listed[0]["n"] if listed else None
        chosen = next((v for v in listed if v["n"] == pick), listed[0] if listed else None)
        # Would a Remake differ from the newest version? Not if it was made
        # from the newest video with the samples there are now.
        stale = []
        if snaps:
            newest = snaps[max(snaps)]
            if state["exists"] and newest["video"] != state["n"]:
                stale.append(f"the final video is newer (v{state['n']}) than the one this transcript is timed by "
                             f"(v{newest['video']})")
            if newest["samples"] != samples_now(db, source):
                stale.append("samples have changed since it was made")
            if newest.get("settings") != voice_settings():
                stale.append("the who's-speaking Settings or the matcher have changed since it was made")
        # The rows, rebuilt from the version's words (older versions too): its
        # turns cut into rows (rows_of) so the page's List can virtualize, each
        # with its first word's paragraph (the Edit link's target). Sent as
        # "turns", though they are rows: a long turn is several.
        turns = []
        if chosen:
            snap = snaps[chosen["n"]]
            whole = turns_of(snap["words"], snap["names"], snap["colors"])
            ids = [x[0] for x in snap["words"]]
            para_of = {}
            for b in range(0, len(ids), 900):  # SQLite's variable limit
                batch = ids[b:b + 900]
                para_of.update(db.execute(f"SELECT id, para FROM words WHERE id IN ({','.join('?' * len(batch))})", batch))
            turns = rows_of(whole, snap["words"], para_of)
            print(f"final-transcript-rows source={source} n={chosen['n']} turns={len(whole)} rows={len(turns)} "
                  f"maxWords={max((len(r['wl']) for r in turns), default=0)}", flush=True)
        self.send_json(200, {"needVideo": not state["exists"], "versions": listed, "version": chosen,
                             "remakeWhy": stale, "upToDate": bool(snaps) and not stale,
                             "editChanged": bool(state["exists"] and not state["current"]),
                             "turns": turns,
                             "videoCurrent": bool(state["exists"] and state["current"]),
                             "matching": matching.get(source)})

    def notes_route(self):
        # GET /notes?source=&n=: the source's notes, each marked placed when
        # transcript version n (else the newest) still has its words at its
        # ids; a stale note is shown but never put on other words.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        source = query.get("source", [""])[0]
        snaps = versions(source, "transcript")
        try:
            snap = dict(snaps).get(int(query.get("n", [""])[0]))
        except ValueError:
            snap = snaps[0][1] if snaps else None
        out = placed_notes(sqlite3.connect(DB), source, snap)
        self.send_json(200, {"notes": out})

    def edit_note(self, op):
        # POST /notes/add {source, from, to, words, markdown}, /notes/update
        # {id, markdown}, /notes/delete {id}: a note on the final transcript
        # (docs/annotation.md). The agent calls add once Jon OKs the Markdown,
        # usually with curl, so a body that isn't JSON gets a 400 saying so.
        try:
            body = self.json_body()
        except ValueError:
            return self.send_json(400, {"error": "the body isn't JSON"})
        if not isinstance(body, dict):
            return self.send_json(400, {"error": "expected a JSON object"})
        db = sqlite3.connect(DB)
        if op == "add":
            source, a, b = body.get("source"), body.get("from"), body.get("to")
            words, md = body.get("words"), body.get("markdown")
            if not isinstance(source, str) or not source:
                return self.send_json(400, {"error": "expected source"})
            if not all(isinstance(x, int) and not isinstance(x, bool) for x in (a, b)) or a > b:
                return self.send_json(400, {"error": "expected integer from <= to"})
            if not isinstance(words, str) or not words.strip():
                return self.send_json(400, {"error": "expected words"})
            if not isinstance(md, str) or not md.strip() or len(md) > 20000:
                return self.send_json(400, {"error": "expected markdown, 1 to 20000 characters"})
            for x in (a, b):
                if not db.execute("SELECT 1 FROM words WHERE id = ? AND source = ? AND kept = 1",
                                  (x, source)).fetchone():
                    return self.send_json(400, {"error": f"{x} is not a kept word of {source}"})
            prov = body.get("provisional", False)
            if not isinstance(prov, bool):
                return self.send_json(400, {"error": "provisional must be true or false"})
            now = time.time()
            with db:
                id_ = db.execute("INSERT INTO notes (source, w_from, w_to, words, markdown, created_at, updated_at, provisional) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (source, a, b, words, md, now, now, int(prov))).lastrowid
            print(f"notes add source={source} id={id_} from={a} to={b} chars={len(md)} provisional={prov}", flush=True)
        else:
            id_ = body.get("id")
            row = db.execute("SELECT source, w_from, w_to FROM notes WHERE id = ?", (id_,)).fetchone() \
                if isinstance(id_, int) else None
            if not row:
                return self.send_json(404, {"error": "no such note"})
            source, a, b = row
            if op == "update":
                # Markdown, or Accept ({provisional: false}), or both. Only
                # Accept changes provisional, so it can't be set back to true.
                if "markdown" not in body and "provisional" not in body:
                    return self.send_json(400, {"error": "expected markdown or provisional"})
                sets, args, changed = [], [], []
                if "markdown" in body:
                    md = body["markdown"]
                    if not isinstance(md, str) or not md.strip() or len(md) > 20000:
                        return self.send_json(400, {"error": "expected markdown, 1 to 20000 characters"})
                    sets.append("markdown = ?"); args.append(md); changed.append(f"markdown chars={len(md)}")
                if "provisional" in body:
                    if body["provisional"] is not False:
                        return self.send_json(400, {"error": "provisional can only be cleared"})
                    sets.append("provisional = 0"); changed.append("provisional=False")
                with db:
                    db.execute(f"UPDATE notes SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                               (*args, time.time(), id_))
                print(f"notes update source={source} id={id_} from={a} to={b} {' '.join(changed)}", flush=True)
            else:
                with db:
                    db.execute("DELETE FROM notes WHERE id = ?", (id_,))
                print(f"notes delete source={source} id={id_} from={a} to={b}", flush=True)
        push({"notes": source})
        self.send_json(200, {"ok": True, "id": id_})

    def delete_final_route(self):
        # POST /podcast/final/delete or /podcast/final-transcript/delete
        # {source, n}: one version (delete_final).
        body = self.json_body()
        source, kind = body.get("source") or "", "transcript" if "transcript" in self.path else "video"
        try:
            n = int(body.get("n"))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "expected n"})
        if kind == "video" and source in finals:
            return self.send_json(409, {"error": "a final video is being made"})
        if not delete_final(source, kind, n):
            return self.send_json(404, {"error": f"no {kind} v{n}"})
        notify("exports")
        push({"finalDeleted": source, "kind": kind, "n": n})
        self.send_json(200, {"deleted": n, "kind": kind})

    def start_final_transcript(self):
        # {source}: match voices, then the final transcript's Markdown
        # (make_final_transcript), in the background. Needs a final video
        # (its times) and the speaker model; one match at a time, as before.
        source = self.json_body().get("source") or ""
        db = sqlite3.connect(DB)
        if not versions(source, "video"):
            return self.send_json(409, {"error": "make the final video first: the transcript is timed by it"})
        if not (os.path.isfile(SPEECH_PYTHON) and os.path.isfile(SPEAKER_MODEL)):
            return self.send_json(409, {"error": f"the speaker model isn't installed ({SPEAKER_MODEL})"})
        if not db.execute("SELECT 1 FROM words WHERE source = ? AND speaker_how = 'tag' AND kept = 1 LIMIT 1", (source,)).fetchone():
            return self.send_json(409, {"error": "no samples yet: mark some of each speaker's voice in the Workspace"})
        with TRANSCRIBE_LOCK:
            if matching:
                return self.send_json(409, {"error": "voices are already being matched"})
            matching[source] = {"done": 0, "total": 0}
        threading.Thread(target=make_final_transcript, args=(source,), daemon=True).start()
        push({"matching": source, "done": 0, "total": 0})
        self.send_json(200, {"source": source, "making": True})

    def start_check(self):
        # {source, t0, t1}: the round-trip check (check_text) over that
        # stretch of the source, the whole of it when not given; in the
        # background, one at a time, and not while whisper is transcribing.
        body = self.json_body()
        source = body.get("source") or ""
        db = sqlite3.connect(DB)
        if not db.execute("SELECT 1 FROM transcripts WHERE source = ? AND status = 'done'", (source,)).fetchone():
            return self.send_json(404, {"error": f"no finished transcript of {source!r}"})
        with TRANSCRIBE_LOCK:
            if transcribing or checking:
                return self.send_json(409, {"error": "whisper is busy (transcribing or checking); try again when it's done"})
            checking[source] = {"started": datetime_now()}
        lo, hi = float(body.get("t0") or 0), float(body.get("t1") or 0)
        print(f"check start source={source} range={lo:.0f}-{hi or 0:.0f}", flush=True)
        threading.Thread(target=check_text, args=(source, lo, hi), daemon=True).start()
        push({"checking": source, "done": False})
        self.send_json(200, {"source": source, "checking": True})

    def edit_text(self, op):
        # edit {source, text}: save the edited text (apply_text_edit);
        # undo {source}: take back the last change; restore {source, ids}:
        # put cut words back (a Cuts row's Restore). Each answers with the new
        # text, so the page can show it.
        body = self.json_body()
        source = body.get("source") or ""
        db = sqlite3.connect(DB)
        with TEXT_LOCK:
            if not ensure_words(db, source):
                return self.send_json(404, {"error": f"no transcript of {source!r}"})
            if op == "edit":
                if not isinstance(body.get("text"), str):
                    return self.send_json(400, {"error": "expected {source, text}"})
                para = body.get("para")
                result = apply_text_edit(db, source, body["text"], None if para is None else int(para))
            elif op == "undo":
                ch = undo_text_edit(db, source)
                result = {"undone": bool(ch), "tagged": len(ch.get("tagged", [])) if ch else 0,
                          "cut": len(ch["restored"]) if ch else 0,
                          "restored": len(ch["cut"]) if ch else 0, "corrected": len(ch["corrected"]) if ch else 0,
                          "para": describe_step(db, source, ch)[1] if ch else None,
                          "did": "Undid " + describe_step(db, source, ch)[0] if ch else ""}
            elif op == "redo":
                ch = redo_text_edit(db, source)
                result = {"redone": bool(ch), "tagged": len(ch.get("tagged", [])) if ch else 0,
                          "cut": len(ch["cut"]) if ch else 0,
                          "restored": len(ch["restored"]) if ch else 0, "corrected": len(ch["corrected"]) if ch else 0,
                          "para": describe_step(db, source, ch)[1] if ch else None,
                          "did": "Redid " + describe_step(db, source, ch)[0] if ch else ""}
            elif op == "replace":
                if not isinstance(body.get("before"), str) or not isinstance(body.get("after"), str):
                    return self.send_json(400, {"error": "expected {source, before, after}"})
                result = replace_words(db, source, body["before"].strip(), body["after"].strip())
                if "error" in result:
                    return self.send_json(400, result)
            elif op == "elide":
                para = body.get("para")
                result = elide_tics(db, source, dry=bool(body.get("dryRun")), para=None if para is None else int(para))
                result.pop("ids", None)
                result["cut"] = 0 if body.get("dryRun") else result["fillers"] + result["stutters"]
                print(f"elide source={source} fillers={result['fillers']} stutters={result['stutters']}"
                      f"{' dry' if body.get('dryRun') else ''}", flush=True)
            else:
                # {ids} restores those cut words; {reason: 'tic'} every word
                # cut as that ("Restore all tics").
                if body.get("reason"):
                    only, scope = ("", ()) if body.get("para") is None else (" AND para = ?", (int(body["para"]),))
                    cut = [r[0] for r in db.execute("SELECT id FROM words WHERE source = ? AND kept = 0 "
                                                    f"AND cut_reason = ?{only}", (source, body["reason"], *scope))]
                else:
                    ids = [int(i) for i in body.get("ids") or []]
                    cut = [r[0] for r in db.execute(
                        f"SELECT id FROM words WHERE source = ? AND kept = 0 AND id IN ({','.join('?' * len(ids))})",
                        [source, *ids])] if ids else []
                record_text_change(db, source, {"cut": [], "restored": cut, "corrected": []})
                result = {"restored": len(cut)}
            db.commit()
            print(f"text {op} source={source} {json.dumps(result)}", flush=True)
            result.update(text_summary(db, source))
        notify("words")
        if result.get("cut") or result.get("restored"):
            align_soon(source)
        self.send_json(200, result)

    def json_body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def start_transcript(self):
        # {source, prompt, replace}: transcribe a listed source in the
        # background (run_transcript). Refused while another runs, and, when
        # this source's segments already have speakers, unless replace is
        # true: a new transcript has new segments, so the labels would go.
        body = self.json_body()
        source, prompt = body.get("source") or "", " ".join(str(body.get("prompt") or "").split())
        path = source_paths().get(source)
        if not path:
            return self.send_json(404, {"error": f"no source named {source!r}"})
        model = transcribe_model()
        if not model:
            return self.send_json(409, {"error": f"no whisper model in {WHISPER_MODELS}"})
        db = sqlite3.connect(DB)
        labeled = db.execute("SELECT count(*) FROM segments WHERE source = ? AND speaker_id IS NOT NULL",
                             (source,)).fetchone()[0]
        if labeled and not body.get("replace"):
            return self.send_json(409, {"labeled": labeled,
                                        "error": f"{labeled} segments have speakers; transcribing again loses them"})
        # A new transcript has new words, so it would discard text editing
        # (cuts, corrections, the undo history): refused unless replace.
        edited = db.execute("SELECT count(*) FROM words WHERE source = ? AND (kept = 0 OR shown IS NOT NULL)",
                            (source,)).fetchone()[0]
        if edited and not body.get("replace"):
            return self.send_json(409, {"edited": edited,
                                        "error": f"{edited} words are cut or corrected; transcribing again loses that editing"})
        with TRANSCRIBE_LOCK:
            if transcribing or checking:
                return self.send_json(409, {"error": f"whisper is busy with {next(iter(transcribing or checking))}"})
            duration = float(probe(path, "format=duration") or 0)
            transcribing[source] = {"done_s": 0, "duration_s": duration}
        # No names given (the page has no box for them now): whisper gets the
        # project's speakers (Settings › Speakers), which gets their names
        # spelled right; on a new project there are usually none yet.
        from_speakers = False
        if not prompt:
            names = [n for (n,) in db.execute("SELECT name FROM speakers ORDER BY position, id") if n]
            if names:
                prompt, from_speakers = ", ".join(names) + ".", True
        db.execute("INSERT OR REPLACE INTO transcripts (source, model, prompt, status, started_at, duration_s) "
                   "VALUES (?, ?, ?, 'running', ?, ?)",
                   (source, os.path.basename(model), prompt, datetime_now(), duration))
        db.commit()
        print(f"transcribe start source={source} model={os.path.basename(model)} duration={duration:.0f} "
              f"prompt={prompt!r}{' (from speakers)' if from_speakers else ''}", flush=True)
        threading.Thread(target=run_transcript, args=(source, path, prompt, model), daemon=True).start()
        notify("transcripts")
        self.send_json(200, {"source": source, "model": os.path.basename(model), "duration_s": duration})

    def transcript_status(self):
        source = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("source", [""])[0]
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM transcripts WHERE source = ?", (source,)).fetchone()
        result = dict(row) if row else {"source": source, "status": None}
        with TRANSCRIBE_LOCK:
            result.update(transcribing.get(source, {}))
        result["segments"] = db.execute("SELECT count(*) FROM segments WHERE source = ?", (source,)).fetchone()[0]
        self.send_json(200, result)

    def edit_speaker(self, op):
        # add {name}, rename {id, name}, delete {id} (its segments go back to
        # no speaker).
        body = self.json_body()
        name = " ".join(str(body.get("name") or "").split())
        db = sqlite3.connect(DB)
        if op in ("add", "rename") and not name:
            return self.send_json(400, {"error": "a speaker needs a name"})
        if op == "add":
            result = {"id": add_speaker(db, name), "name": name}
        elif op == "rename":
            if not db.execute("UPDATE speakers SET name = ? WHERE id = ?", (name, body.get("id"))).rowcount:
                return self.send_json(404, {"error": f"no speaker with id {body.get('id')}"})
            result = {"id": body.get("id"), "name": name}
        else:
            db.execute("UPDATE segments SET speaker_id = NULL WHERE speaker_id = ?", (body.get("id"),))
            db.execute("UPDATE words SET speaker_id = NULL, speaker_how = NULL, speaker_unsure = NULL "
                       "WHERE speaker_id = ?", (body.get("id"),))
            if not db.execute("DELETE FROM speakers WHERE id = ?", (body.get("id"),)).rowcount:
                return self.send_json(404, {"error": f"no speaker with id {body.get('id')}"})
            result = {"id": body.get("id")}
        db.commit()
        print(f"speakers {op} {json.dumps(result)}", flush=True)
        notify("speakers")
        self.send_json(200, result)

    def label_segments(self):
        # {speaker_id (null clears), and either ids: [...] or source, t0, t1
        # (every segment of that source starting in [t0, t1))}. The range form
        # is for labeling a stretch by time, as the agent does from content.
        body = self.json_body()
        speaker = body.get("speaker_id")
        db = sqlite3.connect(DB)
        if speaker is not None and not db.execute("SELECT 1 FROM speakers WHERE id = ?", (speaker,)).fetchone():
            return self.send_json(404, {"error": f"no speaker with id {speaker}"})
        if isinstance(body.get("ids"), list):
            ids = [int(i) for i in body["ids"]]
            n = db.executemany("UPDATE segments SET speaker_id = ? WHERE id = ?",
                               [(speaker, i) for i in ids]).rowcount if ids else 0
            how = f"ids={len(ids)}"
        elif body.get("source") is not None and body.get("t0") is not None and body.get("t1") is not None:
            n = db.execute("UPDATE segments SET speaker_id = ? WHERE source = ? AND t0 >= ? AND t0 < ?",
                           (speaker, body["source"], float(body["t0"]), float(body["t1"]))).rowcount
            how = f"source={body['source']} t0={body['t0']} t1={body['t1']}"
        else:
            return self.send_json(400, {"error": "expected ids, or source, t0 and t1"})
        db.commit()
        print(f"segments speaker={speaker} {how} changed={n}", flush=True)
        notify("speakers")
        self.send_json(200, {"changed": n})

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
        try:
            os.remove(os.path.join(os.path.dirname(src), f".{name}.scenes.json"))
        except OSError:
            pass
        report = os.path.join(os.path.dirname(src), f".{name}.json")
        if os.path.exists(report):
            shutil.move(report, os.path.join(trash, f".{os.path.basename(dest)}.json"))
        self.send_json(200, {"deleted": name, "trashed": f".trash/{os.path.basename(dest)}"})

    def claim_dictation(self):
        # {key}: the page is about to dictate into that row's note. One at a
        # time, and never while a narration or a voice test has the
        # mic. The same row claiming again is fine (the page re-claims when
        # its event stream reconnects mid-dictation).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        key = str(body.get("key", ""))
        with DICTATION_LOCK:
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
        # streams (the usual case: scenes clipped from one source) join by stream
        # copy; otherwise re-encode, fitting each take to the first one's size.
        if not exporting.acquire(blocking=False):
            return self.send_json(409, {"error": "an export is already running"})
        try:
            db = sqlite3.connect(DB)
            # What each scene's list shows is what goes out: a scene whose
            # items differ from what was last burned in (overlay.APPLIED_SQL
            # against takes.overlay_applied) is burned in first, one at a
            # time; a failure exports nothing.
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
            # What it was made from, for the Final video page's "the scenes
            # have changed since" (scenes_key, after any burn-in above).
            prior = [v["n"] for v in screencast_finals(db)["versions"] if v["name"] != name]
            with open(os.path.join(outdir, f".{name}.scenes.json"), "w") as fh:
                json.dump({"scenes": scenes_key(db), "madeAt": datetime_now(), "secs": round(seconds, 1),
                           "n": max(prior, default=0) + 1}, fh)
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
        # the editor shows a placeholder (overlay.sprite) and burning in skips it.
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
        if take_editing(take_id):
            return self.send_json(409, {"error": BUSY_TAKE})
        kind = "shape" if shape else "callout"
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        take = db.execute("SELECT file FROM takes WHERE id = ?", (take_id,)).fetchone()
        if not take:
            return self.send_json(404, {"error": f"no take with id {take_id}"})
        item = lambda i: (lambda r: dict(r) if r else None)(
            db.execute("SELECT * FROM overlays WHERE id = ?", (i,)).fetchone())
        # Each change is a step of the scene's history (record_annot_step).
        if update:
            before = item(body.get("id"))
            n = db.execute("UPDATE overlays SET kind = ?, text = ?, x1 = ?, y1 = ?, x2 = ?, y2 = ?, t_in = ?, "
                           "t_out = ?, tail = ?, shape = ? WHERE id = ? AND take_id = ? AND kind IN ('callout', 'shape')",
                           (kind, text, *box, t_in, t_out, tail, shape, body.get("id"), take_id)).rowcount
            db.commit()
            if not n:
                return self.send_json(404, {"error": "no such callout"})
            after = item(body.get("id"))
            if after != before:  # a save that changed nothing isn't a step
                record_annot_step(take["file"], "update", body.get("id"), before, after)
            return self.send_json(200, {"id": body.get("id")})
        cur = db.execute("INSERT INTO overlays (take_id, kind, text, x1, y1, x2, y2, t_in, t_out, tail, shape) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (take_id, kind, text, *box, t_in, t_out, tail, shape))
        db.commit()
        record_annot_step(take["file"], "add", cur.lastrowid, None, item(cur.lastrowid))
        self.send_json(200, {"id": cur.lastrowid})

    def callout_sprite(self):
        # The callout being edited as a transparent image of just its box,
        # tail and text (overlay.sprite, the drawing Export burns in) plus where it
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
        # A step of the scene's history too (record_annot_step), so Undo
        # brings the item back.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        before = db.execute("SELECT * FROM overlays WHERE id = ? AND kind IN ('callout', 'shape')",
                            (body.get("id"),)).fetchone()
        if not before:
            return self.send_json(404, {"error": "no such callout"})
        if take_editing(before["take_id"]):
            return self.send_json(409, {"error": BUSY_TAKE})
        before = dict(before)
        db.execute("DELETE FROM overlays WHERE id = ?", (body.get("id"),))
        db.commit()
        take = db.execute("SELECT file FROM takes WHERE id = ?", (before["take_id"],)).fetchone()
        if take:
            record_annot_step(take["file"], "delete", body.get("id"), before, None)
        self.send_json(200, {"deleted": body.get("id")})

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
        # movie, the capture view's Make take. Encoded as live-recorded takes
        # were (libx264 crf 16, 25 fps; AAC 160k, 48 kHz stereo), so Export can
        # still join old takes and new scenes by stream copy. The source's own audio comes along; a
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
        # With sound, its lossless audio comes straight from the source too
        # (audio_master); without, the first edit makes it from the silence.
        os.makedirs(AUDIO, exist_ok=True)
        flac = os.path.join(AUDIO, f".{fname[:-4]}.flac")
        lossless = (["-map", "0:a:0", *mono, "-c:a", "flac", "-ar", "48000", "-ac", "2", flac]
                    if has_audio else [])
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                            "-i", path, *audio_in, "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-tune", "stillimage",
                            "-pix_fmt", "yuv420p", "-r", "25",
                            *mono, *AAC, "-shortest", "-f", "mp4", tmp, *lossless],
                           capture_output=True, text=True)
        if r.returncode:
            for p in (tmp, flac):
                if os.path.exists(p):
                    os.remove(p)
            return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["the clip failed"])[-1]})
        os.replace(tmp, dest)
        if has_audio:
            save_master(fname, flac)
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
        # only by Export. A scene has one when a callout starts at 0 s reaching
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
        # for Undo (undo_take). With dryRun, only says what the cut would
        # remove (cut_drops), for the Cut button's warning.
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
        if body.get("dryRun"):
            # What the cut would remove, for the page's warning; nothing changes.
            return self.send_json(200, {"dryRun": True, "start": round(start, 3), "end": round(end, 3),
                                        **cut_drops(db, row["id"], start, end)})
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
        chain = achain = ""
        for i, (a, b) in enumerate(segs):
            fades = ([f"afade=t=out:st={b - a - 0.01:.3f}:d=0.01"] if i < len(segs) - 1 else []) + \
                    (["afade=t=in:d=0.01"] if i > 0 else [])
            chain += f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
            achain += (f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
                       f"{''.join(',' + x for x in fades)}[a{i}];")
        chain += "".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[v]"
        achain += "".join(f"[a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=0:a=1[a]"
        retime, narr, pz, cz = cut_rules(start, end)
        return self.apply_take_edit(db, row, src, chain, kind, [round(start, 3), round(end, 3)], retime,
                                    audio=(achain, []), narr=narr, pz=pz, cz=cz,
                                    seam=lambda path, tmp, audio: seam_cut(path, start, end, tmp, audio))

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
        # The sound: the scene's from its lossless audio, the stretch's from
        # the scene's before-cut copy (no lossless audio of it is kept).
        chain = achain = ""
        for i, (inp, x, y) in enumerate(segs):
            fades = ([f"afade=t=out:st={y - x - 0.01:.3f}:d=0.01"] if i < len(segs) - 1 else []) + \
                    (["afade=t=in:d=0.01"] if i > 0 else [])
            chain += f"[{inp}:v]trim=start={x:.3f}:end={y:.3f},setpts=PTS-STARTPTS[v{i}];"
            achain += (f"[{inp}:a]atrim=start={x:.3f}:end={y:.3f},asetpts=PTS-STARTPTS,"
                       f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
                       f"{''.join(',' + f for f in fades)}[a{i}];")
        chain += "".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[v]"
        achain += "".join(f"[a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=0:a=1[a]"
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

        # Seam-only: the scene up to the seam, the stretch from that file's
        # own before-cut copy, the rest.
        def seam(path, tmp, audio):
            return seam_splice([("span", path, 0.0, q), ("span", before[path], a, a + secs),
                                ("span", path, q, None)], tmp, audio)

        done = self.apply_take_edit(db, row, src, chain, "uncut", [round(q, 3), round(secs, 3)], retime,
                                    audio=(achain, [before[src]]), extra=lambda path: [before[path]],
                                    narr=narr, pz=pz, cz=cz, seam=seam)
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
            chain = f"[0:v]tpad=start_mode=clone:start_duration={secs:.3f}[v]"
            achain = f"{quiet}[0:a]{fmt},afade=t=in:d=0.01[a1];[as][a1]concat=n=2:v=0:a=1[a]"
        elif at > dur - 0.02:  # hold the last frame
            chain = f"[0:v]tpad=stop_mode=clone:stop_duration={secs:.3f}[v]"
            achain = (f"{quiet}[0:a]{fmt},afade=t=out:st={max(0, dur - 0.01):.3f}:d=0.01[a0];"
                      "[a0][as]concat=n=2:v=0:a=1[a]")
        else:
            chain = (f"[0:v]trim=start=0:end={at:.3f},setpts=PTS-STARTPTS,"
                     f"tpad=stop_mode=clone:stop_duration={secs:.3f}[v0];"
                     f"[0:v]trim=start={at:.3f},setpts=PTS-STARTPTS[v1];[v0][v1]concat=n=2:v=1:a=0[v]")
            achain = (f"[0:a]atrim=start=0:end={at:.3f},asetpts=PTS-STARTPTS,{fmt},"
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

        # Seam-only: the scene up to `at`, its frame there held, the rest.
        def seam(path, tmp, audio):
            return seam_splice([("span", path, 0.0, at), ("hold", path, at, secs), ("span", path, at, None)],
                               tmp, audio)

        done = self.apply_take_edit(db, row, src, chain, "pause", [round(at, 3), round(secs, 3)], retime,
                                    audio=(achain, []), narr=narr, pz=pz, cz=cz, seam=seam)
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
        # One at a time, and never while a note is dictated or a voice test
        # runs. The start line logs how long the mic took to go live (from
        # this request to the recorder's first samples), the ffprobe before
        # it, and whether the recorder was rebuilt: the countdown waits on
        # all of it (2026-10-13: ~1.9 s, then the page's own ~1.4 s).
        arrived = time.time()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM takes WHERE id = ?", (body.get("id"),)).fetchone()
        if not row:
            return self.send_json(404, {"error": f"no take with id {body.get('id')}"})
        if take_editing(row["id"]):
            return self.send_json(409, {"error": BUSY_TAKE})
        src = os.path.join(ROOT, row["file"])
        if not os.path.isfile(src):
            return self.send_json(404, {"error": f"{row['file']} is missing"})
        try:
            start, end = float(body.get("start")), float(body.get("end"))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "start and end must be numbers"})
        probe_t = time.time()
        dur = float(probe(src, "format=duration") or 0)
        probe_t = time.time() - probe_t
        start, end = max(0.0, start), min(dur, end)
        if end - start < 0.5:
            return self.send_json(400, {"error": "the clip to narrate over must be at least 0.5 s"})
        built = False
        if not os.path.exists(NATIVE_BIN) or os.path.getmtime(NATIVE_BIN) < os.path.getmtime(NATIVE_SRC):
            built = True
            os.makedirs(os.path.dirname(NATIVE_BIN), exist_ok=True)
            r = subprocess.run(["swiftc", "-O", "-o", NATIVE_BIN, NATIVE_SRC], capture_output=True, text=True)
            if r.returncode:
                return self.send_json(500, {"error": "couldn't build the recorder (record_native.swift)"})
        with DICTATION_LOCK:
            if testing.locked():
                return self.send_json(409, {"error": "the mic is busy (testing)"})
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
            # narration's time zero.
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
        print(f"narrate: start take {n['id']} {start:.2f}-{end:.2f}s, mic live after {n['t0'] - arrived:.2f}s "
              f"(ffprobe {probe_t:.2f}s, recorder built: {'yes' if built else 'no'})", flush=True)
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
            db = sqlite3.connect(DB)
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM takes WHERE id = ?", (n["id"],)).fetchone()
            src = os.path.join(ROOT, row["file"]) if row else ""
            if not os.path.isfile(src):
                return self.send_json(409, {"error": "the take is gone"})
            start, end = n["start"], n["end"]
            # Keep the audio being replaced, so this narration can be removed
            # later (/narrate/remove) whatever else has been done to the take:
            # saved alongside the voice's processing, not after it. Each step's
            # time is logged (narrate: ... steps), to see what a narration's
            # stop is made of.
            os.makedirs(NARRATED, exist_ok=True)
            kept = f"{row['file'][:-4]}-{time.strftime('%Y%m%d-%H%M%S')}.wav"
            steps, t = {}, time.time()
            master = audio_master(row["file"])
            steps["lossless audio"], t = time.time() - t, time.time()
            kept_job = ThreadPoolExecutor(1)
            kept_run = kept_job.submit(subprocess.run, [
                "ffmpeg", "-v", "error", "-y", "-i", master, "-vn", "-af",
                f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS",
                "-ar", "48000", "-ac", "2", os.path.join(NARRATED, kept)], capture_output=True, text=True)
            kept_job.shutdown(wait=False)

            def drop_kept():
                kept_run.result()
                if os.path.exists(os.path.join(NARRATED, kept)):
                    os.remove(os.path.join(NARRATED, kept))
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{offset:.3f}", "-t", f"{said:.3f}",
                                "-i", n["wav"], "-af", NARRATE_AF, "-ar", "48000", "-ac", "1", voice],
                               capture_output=True, text=True)
            steps["voice"], t = time.time() - t, time.time()
            if r.returncode:
                drop_kept()
                return self.send_json(500, {"error": (r.stderr.strip().splitlines() or ["narration failed"])[-1]})
            m = subprocess.run(["ffmpeg", "-v", "info", "-i", voice, "-af", "loudnorm=print_format=json",
                                "-f", "null", "-"], capture_output=True, text=True).stderr
            steps["loudness"], t = time.time() - t, time.time()
            ln = json.loads(m[m.rindex("{"):m.rindex("}") + 1])
            loud = float("-inf") if "inf" in ln["input_i"] else float(ln["input_i"])
            print(f"narrate: take {n['id']} offset={offset:.3f}s said={said:.2f}s of {clip:.2f}s "
                  f"measured {ln['input_i']} LUFS, peak {ln['input_tp']} dBTP", flush=True)
            if loud < NARRATE_FLOOR:
                drop_kept()
                return self.send_json(400, {"error": "no speech was heard, so the take is unchanged"})
            gain = -16.0 - loud
            dur = float(probe(src, "format=duration") or 0)
            chain = self.replace_audio_chain(
                start, end, dur, f"volume={gain:.2f}dB,alimiter=limit=0.841:attack=5:release=80:level=false,"
                                 f"afade=t=in:d=0.01,afade=t=out:st={said - 0.01:.3f}:d=0.01")
            if kept_run.result().returncode:
                drop_kept()
                return self.send_json(500, {"error": "couldn't keep the audio being replaced"})
            steps["kept (waited)"], t = time.time() - t, time.time()
            # Narrating over an earlier narration's stretch takes that one's
            # row: its kept audio would wipe this narration too.
            done = self.apply_take_edit(
                db, row, src, None, "narrate", [round(start, 3), round(end, 3)],
                lambda t_in, t_out: (t_in, t_out), audio=(chain, [voice]), copy_video=True,
                narr=lambda a, b: None if start < b - NARR_EDGE and end > a + NARR_EDGE else (a, b))
            steps["remix"] = time.time() - t
            print(f"narrate: take {n['id']} stop steps: "
                  + ", ".join(f"{k} {v:.2f}s" for k, v in steps.items()), flush=True)
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
        done = self.apply_take_edit(db, row, src, None, "unnarrate", [round(start, 3), round(end, 3)],
                                    lambda t_in, t_out: (t_in, t_out), audio=(chain, [kept]), copy_video=True)
        if done:
            db.execute("DELETE FROM narrations WHERE id = ?", (n["id"],))
            db.commit()
            self.send_json(200, {"id": row["id"], "name": row["name"], "edit": "unnarrate", "start": round(start, 2),
                                 "end": round(end, 2), "duration": done[0], "undo": done[1]})

    def apply_take_edit(self, db, row, src, *args, **kwargs):
        # One edit per take at a time (claim_take); a second gets 409.
        if not claim_take(row["id"]):
            self.send_json(409, {"error": BUSY_TAKE})
            return None
        try:
            return self._apply_take_edit(db, row, src, *args, **kwargs)
        finally:
            release_take(row["id"])

    def _apply_take_edit(self, db, row, src, chain, kind, record, retime, audio, extra=(), copy_video=False,
                         narr=None, pz=None, cz=None, seam=None):
        # The part a cut, a pause, a narration and its removal share. The
        # sound: audio = (achain, inputs), a filter chain ending in [a] whose
        # input 0 is the take's lossless audio (audio_master) and 1… are
        # `inputs`, run once into the take's next lossless audio and one AAC
        # file. The picture: the ffmpeg filter `chain` (ending in [v]) on the
        # take's MP4 and its clean copy, `extra` being further input files
        # (inputs 1…); with copy_video the picture is stream-copied. Each file
        # gets the same AAC, into hidden temp files first, so a failure
        # changes nothing. Moves the originals to media/.trash/ with a sidecar
        # (<stem>-before-<kind>-<when>.json: the take's row, its overlays,
        # `record`), swaps the new files in, retimes the overlays with
        # retime(t_in, t_out), and updates the take's row. Returns (new
        # duration, sidecar name), or None after sending an error.
        clean = os.path.join(ROOT, ".clean", row["file"])
        targets = [src] + ([clean] if os.path.isfile(clean) else [])
        stem = row["file"][:-4]
        next_flac = os.path.join(AUDIO, f".{stem}.{kind}.flac")
        next_aac = os.path.join(AUDIO, f".{stem}.{kind}.m4a")

        def drop_temps(*paths):
            for t in (next_flac, next_aac, *paths):
                if os.path.exists(t):
                    os.remove(t)

        try:
            master = audio_master(row["file"])
        except Exception as e:
            self.send_json(500, {"error": f"the take's lossless audio: {e}"})
            return None

        audio_t = {}

        def make_audio():
            t = time.time()
            achain, inputs = audio
            ins = [a for p in (master, *inputs) for a in ("-i", p)]
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex",
                                f"{achain};[a]asplit=2[af][ac]", "-map", "[af]", "-c:a", "flac", "-ar", "48000",
                                "-ac", "2", next_flac, "-map", "[ac]", *AAC, next_aac],
                               capture_output=True, text=True)
            audio_t["s"] = time.time() - t
            if r.returncode:
                raise RuntimeError((r.stderr.strip().splitlines() or [f"{kind}: the audio failed"])[-1])
            return next_aac

        # The sound is made alongside the picture; edit_audio() waits for it.
        audio_job = ThreadPoolExecutor(1)
        audio_run = audio_job.submit(make_audio)
        audio_job.shutdown(wait=False)
        edit_audio = audio_run.result

        # One file's edit, into its temp file. The take and its clean copy
        # are done at once (they were one after the other, each a full
        # re-encode: ~7 s for a 43 s scene). An edit given seam (a function of
        # the file, its temp file and edit_audio: a cut, a pause, removing
        # one, restoring a cut) tries it first, re-encoding only around the
        # seams, and falls back to the chain if it can't.
        def edit_one(path):
            tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)[:-4]}.{kind}.mp4")
            if seam:
                try:
                    return tmp, None, f"seam-only, {seam(path, tmp, edit_audio)} frames re-encoded"
                except Exception as e:
                    if audio_run.done() and audio_run.exception():
                        return tmp, str(audio_run.exception()), ""
                    print(f"edit: seam-only {kind} of {os.path.basename(path)} failed ({e}); full re-encode",
                          flush=True)
            try:
                aac = edit_audio()
            except Exception as e:
                return tmp, str(e), ""
            if copy_video:
                cmd = ["-i", path, "-i", aac, "-map", "0:v", "-map", "1:a", "-c", "copy"]
            else:
                # extra: further inputs, the same for both files, or a function
                # giving each file its own (a restored cut reads the scene's and
                # the clean copy's own before-cut files).
                ex = extra(path) if callable(extra) else extra
                cmd = [*[a for p in (path, *ex) for a in ("-i", p)], "-i", aac, "-filter_complex", chain,
                       "-map", "[v]", *overlay.ENC, "-map", f"{len(ex) + 1}:a", "-c:a", "copy"]
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", *cmd, tmp], capture_output=True, text=True)
            err = (r.stderr.strip().splitlines() or [f"{kind} failed"])[-1] if r.returncode else None
            return tmp, err, "video copied" if copy_video else "full re-encode"

        encode_t = time.time()
        with ThreadPoolExecutor(len(targets)) as pool:
            results = list(pool.map(edit_one, targets))
        encode_t = time.time() - encode_t
        tmps = [t for t, _, _ in results]
        failed = [e for _, e, _ in results if e]
        if failed:
            try:
                audio_run.result()
            except Exception:
                pass
            drop_temps(*tmps)
            self.send_json(500, {"error": failed[0]})
            return None
        print(f"edit: {kind} take {row['id']} {len(targets)} file{'s' if len(targets) > 1 else ''} in "
              f"{encode_t:.1f}s ({'; '.join(sorted(set(m for _, _, m in results)))}; "
              f"audio from lossless in {audio_t.get('s', 0):.1f}s)", flush=True)
        trash = os.path.join(ROOT, ".trash")
        os.makedirs(trash, exist_ok=True)
        # A new edit ends the chance to redo (discard_undone).
        with EDIT_LOCK:
            discard_undone(row["file"])
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
        # The sound just made is the take's lossless audio now; the one it was
        # made from isn't kept (Undo rebuilds it from the restored MP4).
        save_master(row["file"], next_flac)
        drop_temps()
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
        # edit; None to take the last one back. Only takes recorded live by
        # the retired record.sh have that JSON (for remix_take.py); a scene
        # cut from a source has none, so there is nothing to mark.
        if not file.endswith("-raw.mp4"):
            return
        meta_path = os.path.join(WORK, "takes", file.replace("-raw.mp4", ".json"))
        if not os.path.isfile(meta_path):
            return
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
        # One edit per take at a time (claim_take): the body is read here for
        # the take's id, then handed on unread to _undo_take.
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            take_id = (json.loads(raw or b"{}") or {}).get("id")
        except ValueError:
            take_id = None
        if take_id is not None and not claim_take(take_id):
            return self.send_json(409, {"error": BUSY_TAKE})
        self.rfile = io.BytesIO(raw)
        try:
            return self._undo_take()
        finally:
            if take_id is not None:
                release_take(take_id)

    def _undo_take(self):
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
            if is_row_step(saved):
                # An item's change: only its row goes back.
                set_item_row(db, row["id"], saved["id"], saved.get("before"))
                db.commit()
                saved["undone"] = {"at": time.time()}
                with open(os.path.join(TRASH, name), "w") as f:
                    json.dump(saved, f, indent=1)
                seal_cursor(row["file"])
                return self.send_json(200, {"id": row["id"], "undone": edit_what(name, saved)})
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
            seal_cursor(row["file"])
            self.mark_take_cut(row["file"], None)
        self.send_json(200, {"id": row["id"], "undone": edit_what(name, saved)})

    def redo_take(self):
        # One edit per take at a time (claim_take): the body is read here for
        # the take's id, then handed on unread to _redo_take.
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            take_id = (json.loads(raw or b"{}") or {}).get("id")
        except ValueError:
            take_id = None
        if take_id is not None and not claim_take(take_id):
            return self.send_json(409, {"error": BUSY_TAKE})
        self.rfile = io.BytesIO(raw)
        try:
            return self._redo_take()
        finally:
            if take_id is not None:
                release_take(take_id)

    def _redo_take(self):
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
            if is_row_step(saved):
                set_item_row(db, row["id"], saved["id"], saved.get("after"))
                db.commit()
                del saved["undone"]
                saved["lastAt"] = 0  # a redone step isn't extended by the next change
                with open(os.path.join(TRASH, name), "w") as f:
                    json.dump(saved, f, indent=1)
                return self.send_json(200, {"id": row["id"], "redone": edit_what(name, saved)})
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

    def log_podcast(self):
        # {ev, ...}: one line per paragraph-box event on the Podcast page
        # (focus, blur, with what the page thought it was editing and whether
        # the text changed), so a save that never arrives can be traced to the
        # step that dropped it (2026-11-13: edits weren't saving).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        ev = body.pop("ev", "?")
        print(f"podcast-page: {ev} " + " ".join(f"{k}={json.dumps(v)}" for k, v in body.items()), flush=True)
        self.send_json(200, {})

    def log_undo_gate(self):
        # {enabled, changed, ...inputs}: the take editor's Undo button's
        # inputs changed (the ChangeListener beside undoEdit in Main.xmlui).
        # One line per change, so a disabled Undo can be traced to the input
        # holding it: after a narration, Undo stayed off while the server said
        # canUndo (2026-10-05). Kept as standing instrumentation. Lines can
        # arrive out of order (the page's posts are concurrent): seq is the
        # page's count and pageMs when it saw the change, so sort by seq.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        seq, page_ms = body.pop("seq", None), body.pop("pageMs", None)
        changed = ",".join(str(c) for c in body.pop("changed", []) or [])
        enabled = body.pop("enabled", None)
        fields = " ".join(f"{k}={json.dumps(v)}" for k, v in body.items())
        print(f"undo-gate: seq={json.dumps(seq)} pageMs={json.dumps(page_ms)} "
              f"enabled={json.dumps(enabled)} changed={changed or '-'} {fields}", flush=True)
        self.send_json(200, {"logged": 1})

    def log_page_health(self):
        # {uptimeS, heapMB, videos, nodes}: the page's own measure of itself
        # every 5 minutes (studioPageHealth in index.html, its Timer in
        # Main.xmlui). The page got sluggish after days open and a refresh
        # helped (2026-10-12); growth across these lines names what built up.
        # Kept as standing instrumentation.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        heap = body.get("heapMB")
        print(f"page-health: uptime={body.get('uptimeS')}s "
              f"heap={'-' if heap is None else str(heap) + 'MB'} "
              f"videos={body.get('videos')} nodes={body.get('nodes')}", flush=True)
        # Once per page load (the probe's first success): how long
        # index.html's synchronous studio-port.json read took. Read against
        # Bram's [http] op=self-probe-slow to tell a browser hold-up from
        # Bram's server (page-load-port-fetch-timing; the 23 s stall of
        # 2026-11-15).
        pf = body.get("portFetch")
        if isinstance(pf, dict):
            print(f"page-load: port-fetch ms={pf.get('ms')} status={pf.get('status')} "
                  f"sentAt={pf.get('sentAt')}", flush=True)
        self.send_json(200, {"logged": 1})

    def run_voicetest(self):
        # One 10s test at a time, never during a narration or dictation; returns when it's measured.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if dictation["current"]:
            return self.send_json(409, {"error": dictating_where()})
        if not testing.acquire(blocking=False):
            return self.send_json(409, {"error": "the mic is busy (testing)"})
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
        if narration["current"]:
            return "a narration is being recorded"
        if dictation["current"]:
            return dictating_where()
        if transcribing or checking:
            return f"{next(iter(transcribing or checking))} is being transcribed or checked"
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

    def delete_open_project(self):
        # {slug}: delete the project you're in (Manage › Delete this
        # project). Studio runs on the open project, so: record it in
        # projects/.delete-pending, open the other project changed most
        # recently (its studio.db), and the restarted server moves the
        # pending one to the Trash at startup (finish_pending_delete).
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        slug = body.get("slug")
        if not slug or os.path.join(studio_paths.PROJECTS, str(slug)) != studio_paths.PROJECT:
            return self.send_json(409, {"error": "only the open project is deleted this way"})
        if SAFE_LOCK.locked():
            return self.send_json(409, {"error": "A safekeeping copy is running"})
        others = []
        for n in os.listdir(studio_paths.PROJECTS):
            folder = os.path.join(studio_paths.PROJECTS, n)
            if n != slug and not n.startswith(".") and os.path.isfile(os.path.join(folder, "project.json")):
                db_file = os.path.join(folder, "studio.db")
                others.append((os.path.getmtime(db_file if os.path.isfile(db_file) else folder), n))
        if not others:
            return self.send_json(409, {"error": "It's the only project: make another one first"})
        why = self.busy()
        if why:
            return self.send_json(409, {"error": f"Can't delete the project now: {why}"})
        with open(PENDING_DELETE, "w") as f:
            f.write(slug + "\n")
        print(f"projects: delete pending slug={slug}, opening {max(others)[1]}", flush=True)
        return self.open_project(max(others)[1])

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

    def save_settings(self):
        # {defaultSafekeeping, create}: the Settings page's Save. The folder
        # is checked as a project's own location is (check_safekeeping_folder);
        # missing answers {missing, defaultSafekeeping} without saving, and
        # create: true then makes it. "" forgets the default. Answers the
        # settings as saved.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        settings = read_settings()
        if "defaultSafekeeping" in body:
            loc = str(body.get("defaultSafekeeping") or "").strip()
            if loc:
                loc, err, missing = check_safekeeping_folder(loc, body.get("create"))
                if err:
                    return self.send_json(400, {"error": err})
                if missing:
                    return self.send_json(200, {**settings, "defaultSafekeeping": loc, "missing": True})
            settings["defaultSafekeeping"] = loc
        before = podcast_settings()
        if isinstance(body.get("podcast"), dict):
            podcast = dict(settings.get("podcast", {}))
            for k, v in body["podcast"].items():
                if k in PODCAST_WORD_SETTINGS:
                    podcast[k] = ", ".join(w for w in (x.strip() for x in str(v or "").split(",")) if w)
                    continue
                if k not in PODCAST_SETTINGS:
                    continue
                default, lo, hi = PODCAST_SETTINGS[k]
                try:
                    v = type(default)(v)
                except (TypeError, ValueError):
                    return self.send_json(400, {"error": f"{k} must be a number"})
                if not lo <= v <= hi:
                    return self.send_json(400, {"error": f"{k} must be between {lo} and {hi}"})
                podcast[k] = v
            settings["podcast"] = podcast
        try:
            write_settings(settings)
        except OSError as e:
            return self.send_json(500, {"error": f"Couldn't save the settings: {e}"})
        print(f"settings: {settings}", flush=True)
        after = podcast_settings()
        if (before["paragraphPauseS"], before["paragraphWords"]) != (after["paragraphPauseS"], after["paragraphWords"]):
            # Paragraphs follow the new rule at once in the open project;
            # others get it when next opened (ensure_words).
            db = sqlite3.connect(DB)
            for (src,) in db.execute("SELECT DISTINCT source FROM words").fetchall():
                assign_paras(db, src)
            db.commit()
            print(f"paragraphs reassigned pause={after['paragraphPauseS']} words={after['paragraphWords']}", flush=True)
            notify("words")
        self.send_json(200, {**settings, "podcast": after,
                             "podcastDefaults": {**{k: v[0] for k, v in PODCAST_SETTINGS.items()}, **PODCAST_WORD_SETTINGS}})

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
        loc = str(body.get("location") or "").strip()
        if loc:
            loc, err, missing = check_safekeeping_folder(loc, body.get("create"))
            if err:
                return self.send_json(400, {"error": err})
            if missing:
                return self.send_json(200, {"slug": slug, "location": loc, "missing": True})
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
        loc, _ = safekeeping_location(info)
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
        loc, _ = safekeeping_location(info)
        if not loc:
            return self.send_json(409, {"error": "Set a safekeeping location first (here, or a default on the Settings page)"})
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
        # The open project's recordings and history are in use while a
        # narration is recorded, a scene rendered, or an edit saved.
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

    def rename_project(self):
        # {slug, name}: a project's name, as the Projects page and the header
        # show it (project.json). Its folder keeps its name, so projects/.open,
        # its paths and its safekeeping copies are unchanged. The open one too.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        slug, name = str(body.get("slug") or ""), " ".join(str(body.get("name") or "").split())
        if not name:
            return self.send_json(400, {"error": "a project needs a name"})
        meta = os.path.join(studio_paths.PROJECTS, slug, "project.json")
        if not slug or os.path.basename(slug) != slug or not os.path.isfile(meta):
            return self.send_json(404, {"error": f"no project named {slug!r}"})
        try:
            with open(meta) as f:
                info = json.load(f)
            info["name"] = name
            with open(meta + ".tmp", "w") as f:
                json.dump(info, f, indent=1)
            os.replace(meta + ".tmp", meta)
        except (OSError, ValueError) as e:
            return self.send_json(500, {"error": f"couldn't rename: {e}"})
        self.send_json(200, {"slug": slug, "name": name})

    def new_project(self):
        # {name, genre, source}: a new, empty project folder (studio_paths.py),
        # then open it. The folder is named from the name: lowercase, dashes
        # for the rest.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = str(body.get("name") or "").strip()
        genre, source = body.get("genre"), body.get("source")
        if genre not in GENRES:
            return self.send_json(400, {"error": f"genre must be one of {', '.join(GENRES)}"})
        if source not in sources():
            return self.send_json(400, {"error": f"{source!r} isn't one of the source movies"})
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not slug:
            return self.send_json(400, {"error": "Give the project a name with a letter or digit in it"})
        folder = os.path.join(studio_paths.PROJECTS, slug)
        if os.path.exists(folder):
            return self.send_json(409, {"error": f"There is already a project folder named {slug}"})
        why = self.busy()
        if why:
            return self.send_json(409, {"error": f"Can't open a new project now: {why}"})
        create_project_folder(folder, name, genre, [source])
        self.open_project(slug)

    def add_project_source(self):
        # {source}: add a source movie to the open project's list, once.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        source = body.get("source")
        if source not in sources():
            return self.send_json(400, {"error": f"{source!r} isn't one of the source movies"})
        info = read_project(studio_paths.PROJECT)
        if info is None:
            return self.send_json(409, {"error": "The open project has no project.json"})
        if source not in info.setdefault("sources", []):
            info["sources"].append(source)
            write_project(studio_paths.PROJECT, info)
            notify("projects")
        self.send_json(200, {"sources": info["sources"]})

    def set_trace(self):
        # POST /trace {on}: turn XMLUI's trace capture (the Inspector's
        # data) on or off by rewriting config.json's xmluiConfig.xsVerbose
        # (https://www.xmlui.org/docs/xmlui-config: framework settings under
        # appGlobals are being deprecated, so a copy left there is removed).
        # The engine reads it only at startup, so the page reloads after.
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not isinstance(body.get("on"), bool):
            return self.send_json(400, {"error": "on must be true or false"})
        try:
            with open(CONFIG) as f:
                config = json.load(f)
            config.setdefault("xmluiConfig", {})["xsVerbose"] = body["on"]
            config.get("appGlobals", {}).pop("xsVerbose", None)
            tmp = CONFIG + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            os.replace(tmp, CONFIG)
        except (OSError, ValueError) as e:
            return self.send_json(500, {"error": f"couldn't update config.json: {e}"})
        print(f"trace: xsVerbose {'on' if body['on'] else 'off'} (config.json)", flush=True)
        self.send_json(200, {"on": trace_on()})

    def query(self, path, shared=False):
        # The pages' DataSource dataType="sql" requests: {sql, params} (or
        # bare SQL) in, a JSON array of column-keyed rows out, the same
        # contract as Bram's /query. /query reads the open project's database;
        # /query/shared the repo's (SHARED_DB: the Audio bench's voice_tests,
        # shared by all projects), so the page needn't be served by Bram to
        # read it. Read-only at the engine: opened mode=ro with query_only
        # on, so a page can't change data. A shared database or table that
        # doesn't exist yet (a fresh checkout, a scratch copy) reads as empty.
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = raw.decode("utf-8", "replace")
        sql, params = (body, []) if isinstance(body, str) else (
            (body.get("sql"), body.get("params") or []) if isinstance(body, dict) else (None, []))
        if not isinstance(sql, str) or not sql.strip() or not isinstance(params, list):
            return self.send_json(400, {"error": "expected {sql, params}"})
        if shared and not os.path.isfile(path):
            return self.send_json(200, [])
        try:
            db = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                rows = [{k: LOCAL_URL.sub(f"http://127.0.0.1:{PORT}/", v) if isinstance(v, str) else v
                         for k, v in dict(r).items()} for r in db.execute(sql, params)]
            finally:
                db.close()
        except sqlite3.Error as e:
            if shared and str(e).startswith("no such table"):
                return self.send_json(200, [])
            return self.send_json(400, {"error": str(e)})
        self.send_json(200, rows)

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ensure_open_project()
finish_pending_delete()
migrate_all_genres()
if NO_PROJECT:
    # No project to prepare: leave the fresh checkout untouched (no media/,
    # no tables in the repo's studio.db, nothing to watch) and just answer
    # the no-project routes until /projects/new restarts into the first one.
    serve()
os.makedirs(ROOT, exist_ok=True)
# The callouts query reads overlays, so it must exist before the page asks.
with sqlite3.connect(DB) as _db:
    overlay.ensure_schema(_db)
    _db.execute(NARRATIONS_SQL)
    _db.execute(PAUSES_SQL)
    _db.execute(CUTS_SQL)
    _db.execute(TRANSCRIPTS_SQL)
    _db.execute(SEGMENTS_SQL)
    _db.execute(SPEAKERS_SQL)
    _db.execute(WORDS_SQL)
    _db.execute(TRANSCRIPT_EDITS_SQL)
    _db.execute(WORD_EDGES_SQL)
    _db.execute(NOTES_SQL)
    # notes.provisional arrived later (final-transcript-batch-notes).
    if "provisional" not in [r[1] for r in _db.execute("PRAGMA table_info(notes)")]:
        _db.execute("ALTER TABLE notes ADD COLUMN provisional INTEGER NOT NULL DEFAULT 0")
    # words.para and transcript_edits.undone arrived later (podcast-paragraph-playback).
    if "para" not in [r[1] for r in _db.execute("PRAGMA table_info(words)")]:
        _db.execute("ALTER TABLE words ADD COLUMN para INTEGER")
    for col, kind in (("speaker_how", "TEXT"), ("speaker_unsure", "INTEGER")):
        if col not in [r[1] for r in _db.execute("PRAGMA table_info(words)")]:
            _db.execute(f"ALTER TABLE words ADD COLUMN {col} {kind}")
    if "undone" not in [r[1] for r in _db.execute("PRAGMA table_info(transcript_edits)")]:
        _db.execute("ALTER TABLE transcript_edits ADD COLUMN undone INTEGER NOT NULL DEFAULT 0")
    # A transcript still 'running' was cut off by the server stopping.
    _db.execute("UPDATE transcripts SET status = 'failed: the server stopped while it ran' WHERE status = 'running'")
backfill_studio_files()
threading.Thread(target=watch_changes, daemon=True).start()
serve()
