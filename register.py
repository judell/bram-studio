#!/usr/bin/env python3
"""Add a rendered take to studio.db and media/.

    python3 register.py <mp4> [name] [src_start] [src_end]

Copies the MP4 into media/ and inserts a row into takes; its URL points at
serve_media.py (port 8765).

With no name, or "-", the take is named from its narration: whisper.cpp
transcribes up to the first 10 minutes, and the first 24 spoken words become
the name (the first ~40 go into notes). A take with fewer than 3 words (whisper
invents "you" from silence), or one whisper can't read, is "untitled <file
stem>". WHISPER_MODEL overrides the model path.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("WHISPER_MODEL", os.path.expanduser("~/.local/share/whisper-models/ggml-small.en.bin"))
NAME_WORDS, NOTE_WORDS, MIN_WORDS, LISTEN_S = 24, 40, 3, 600


def narration_words(mp4):
    # Whole-track transcription, not a first-N-seconds window: perf-1's speech
    # starts ~80s in, and silencedetect can't find the onset on these tracks.
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "voice.wav")
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-t", str(LISTEN_S), "-vn", "-ac", "1",
                            "-ar", "16000", wav], check=True)
            text = subprocess.run(["whisper-cli", "-m", MODEL, "-f", wav, "-nt", "-np"],
                                  capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return []
    # Drop non-speech tags like [SOUND] and (gun firing), and dialogue dashes.
    text = re.sub(r"\[[^]]*\]|\([^)]*\)", " ", text)
    words = [w for w in text.split() if w != "-"]
    # On silence whisper invents a word or two ("you", "Thank you."): not a name.
    return words if len(words) >= MIN_WORDS else []


src = sys.argv[1]
name = sys.argv[2] if len(sys.argv) > 2 else "-"
src_start = sys.argv[3] if len(sys.argv) > 3 else None
src_end = sys.argv[4] if len(sys.argv) > 4 else None
fname = os.path.basename(src)
dest = os.path.join(HERE, "media", fname)
if os.path.abspath(src) != dest:
    shutil.copy2(src, dest)
notes = None
if name == "-":
    words = narration_words(dest)
    name = " ".join(words[:NAME_WORDS]).rstrip(".,;:!?") or f"untitled {os.path.splitext(fname)[0]}"
    notes = " ".join(words[:NOTE_WORDS]) or None
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", dest],
                           capture_output=True, text=True).stdout.strip() or 0)
db = sqlite3.connect(os.path.join(HERE, "studio.db"))
db.execute("INSERT INTO takes (name, created_at, duration_s, src_start, src_end, file, url, notes) "
           "VALUES (?,?,?,?,?,?,?,?)",
           (name, datetime.now().strftime("%Y-%m-%d %H:%M"), round(dur, 1), src_start, src_end, fname,
            f"http://127.0.0.1:8765/{fname}", notes))
db.commit()
print(f"registered {name}: {fname} ({dur:.1f}s)")
