#!/usr/bin/env python3
"""Record a 10s voice test and measure it, to find what makes takes scratchy.

    python3 voicetest.py <mic> <recorder>

recorder is one of:
  ffmpeg-raw    what ~/Desktop/video-test/session.sh does (avfoundation -> s16le)
  ffmpeg-async  the same, with aresample=async=1 filling timestamp gaps
  native        record_native.swift (AVFoundation's own file writer, no ffmpeg)

Writes media/tests/<stamp>-<mic>-<recorder>-{raw,leveled}.wav, where
"leveled" is take.py's voice chain, adds a row to voice_tests in studio.db,
and prints the row as JSON.
"""
import array
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import wave
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "media", "tests")
NATIVE_SRC = os.path.join(HERE, "record_native.swift")
NATIVE_BIN = os.path.expanduser("~/.cache/bram-studio/record_native")
RATE, SECONDS = 48000, 10
RECORDERS = ("ffmpeg-raw", "ffmpeg-async", "native")
# take.py's leveling, so "leveled" is what a take would sound like
PRE = "highpass=f=80,acompressor=threshold=-24dB:ratio=3:attack=5:release=120"
LOUD = "loudnorm=I=-16:TP=-1.5:LRA=11"


def devices():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                         capture_output=True, text=True).stderr
    audio = out.split("audio devices:", 1)[-1] if "audio devices:" in out else ""
    return re.findall(r"\] \[\d+\] (.+)", audio)


def record_ffmpeg(mic, raw, gapfill):
    pcm = raw + ".pcm"
    af = ["-af", "aresample=async=1:first_pts=0"] if gapfill else []
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "avfoundation", "-i", f":{mic}", "-t", str(SECONDS),
                    "-ac", "1", "-ar", str(RATE), *af, "-f", "s16le", "-y", pcm], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", pcm, "-y", raw],
                   check=True)
    os.remove(pcm)
    return float(SECONDS)  # -t is media time: this is what the capture claims it spanned


def record_native(mic, raw):
    if not os.path.exists(NATIVE_BIN) or os.path.getmtime(NATIVE_BIN) < os.path.getmtime(NATIVE_SRC):
        os.makedirs(os.path.dirname(NATIVE_BIN), exist_ok=True)
        subprocess.run(["swiftc", "-O", "-o", NATIVE_BIN, NATIVE_SRC], check=True, capture_output=True)
    tmp = raw + ".native.wav"
    out = subprocess.run([NATIVE_BIN, mic, tmp, str(SECONDS)], check=True, capture_output=True, text=True).stdout
    t = dict(line.split() for line in out.splitlines() if line.startswith(("started", "stopped")))
    subprocess.run(["ffmpeg", "-v", "error", "-i", tmp, "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", "-y", raw],
                   check=True)
    os.remove(tmp)
    return float(t["stopped"]) - float(t["started"])  # wall time the writer was running


def analyze(raw):
    with wave.open(raw) as w:
        d = array.array("h", w.readframes(w.getnframes()))
    # a click: a sample-to-sample jump no voice makes, counted once per 50ms
    clicks, last = 0, -RATE
    for i in range(1, len(d)):
        if abs(d[i] - d[i - 1]) > 8000:
            if i - last > RATE // 20:
                clicks += 1
            last = i
    peak = max((abs(x) for x in d), default=0)
    rms = math.sqrt(sum(x * x for x in d) / len(d)) if d else 0
    db = lambda v: round(20 * math.log10(v / 32768), 1) if v else None
    return len(d) / RATE, clicks, db(peak), db(rms)


def level(raw, leveled):
    m = subprocess.run(["ffmpeg", "-v", "info", "-i", raw, "-af", f"{PRE},{LOUD}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    ln = json.loads(m[m.rindex("{"):m.rindex("}") + 1])
    subprocess.run(["ffmpeg", "-v", "error", "-i", raw, "-af",
                    f"{PRE},{LOUD}:measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:"
                    f"measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}:"
                    f"offset={ln['target_offset']}:linear=true", "-ar", str(RATE), "-y", leveled], check=True)


def main():
    mic, recorder = sys.argv[1], sys.argv[2]
    if recorder not in RECORDERS:
        sys.exit(f"recorder must be one of {', '.join(RECORDERS)}")
    if mic not in devices():
        sys.exit(f"no audio device named {mic!r}")
    os.makedirs(TESTS, exist_ok=True)
    now = datetime.now()
    base = f"{now:%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', mic.lower()).strip('-')}-{recorder}"
    raw, leveled = (os.path.join(TESTS, f"{base}-{k}.wav") for k in ("raw", "leveled"))
    if recorder == "native":
        expected = record_native(mic, raw)
    else:
        expected = record_ffmpeg(mic, raw, recorder == "ffmpeg-async")
    captured, clicks, peak_db, rms_db = analyze(raw)
    level(raw, leveled)
    row = {"created_at": now.strftime("%Y-%m-%d %H:%M:%S"), "mic": mic, "recorder": recorder,
           "expected_s": round(expected, 2), "captured_s": round(captured, 2),
           "short_pct": round(100 * (1 - captured / expected), 1), "clicks": clicks,
           "peak_db": peak_db, "rms_db": rms_db,
           "raw_url": f"http://127.0.0.1:8765/tests/{os.path.basename(raw)}",
           "leveled_url": f"http://127.0.0.1:8765/tests/{os.path.basename(leveled)}"}
    db = sqlite3.connect(os.path.join(HERE, "studio.db"))
    db.executescript(open(os.path.join(HERE, "schema.sql")).read())
    db.execute(f"INSERT INTO voice_tests ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    db.commit()
    print(json.dumps(row))


if __name__ == "__main__":
    main()
