#!/usr/bin/env python3
"""Save and render one performance take.

    python3 render.py sessions/<stamp> <go_end_s> <stop_start_s> <source.mp4> \
        --events <events.json>

Copied from ~/Desktop/video-test/take.py so the source movie is an argument
(take.py hardcodes ~/Desktop/bram-sep-23.mp4). The playhead path comes from
the studio's MediaPlayer events (start/play/pause/seeked/ended, each with
currentTime and wallMs), not from QuickTime's polled playhead.log. It still
works in ~/Desktop/video-test: session dirs, takes/ and narrated/ live there.

The take runs from just after "go" to just before "stop" (GAP trimmed on
each side), minus any stretch between a recpause and a recresume event (the
app's Pause/Resume): those are cut from both picture and voice. It saves takes/perf-<n>.wav (the voice), takes/perf-<n>.json
(wall times, source start/stop timecodes, the playhead path), and renders
narrated/perf-<n>.mp4 by replaying the path against the source:
  playing          -> that source range at 1x
  paused           -> the frame held
  jump or scrub    -> a cut (short holds at each scrub position)
Voice is leveled like mix.py (gentle compression, two-pass loudnorm to
-16 LUFS, -1.5 dBTP).
"""
import array
import glob
import json
import os
import re
import subprocess
import sys
import wave

HERE = os.path.expanduser("~/Desktop/video-test")
RATE, GAP, FPS = 48000, 0.2, 25
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-tune", "stillimage",
       "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]

D, go_end, stop_start = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
SRC = os.path.realpath(sys.argv[4])
EVENTS = sys.argv[sys.argv.index("--events") + 1]
D = os.path.join(HERE, D)
t0 = float(open(os.path.join(D, "t0")).read())
a_s, b_s = go_end + GAP, stop_start - GAP
w0, w1 = t0 + a_s, t0 + b_s
n = 1 + max([int(re.search(r"perf-(\d+)\.json$", p).group(1))
             for p in glob.glob(os.path.join(HERE, "takes", "perf-*.json"))] or [0])
name = f"perf-{n}"
os.makedirs(os.path.join(HERE, "takes"), exist_ok=True)
os.makedirs(os.path.join(HERE, "narrated", "parts"), exist_ok=True)

# Events: (wall, position, playing) playhead rows, with playing carried across
# events (seeked keeps it), plus the Pause/Resume recording marks.
rows, marks, playing = [], [], False
for e in json.load(open(EVENTS)):
    if e["event"] in ("recpause", "recresume"):
        marks.append((e["wallMs"] / 1000.0, e["event"]))
        continue
    if e["event"] == "start":
        playing = bool(e.get("playing"))
    elif e["event"] == "play":
        playing = True
    elif e["event"] in ("pause", "ended"):
        playing = False
    elif e["event"] != "seeked":
        continue
    rows.append((e["wallMs"] / 1000.0, float(e["currentTime"]), playing))

# Kept intervals: the take window minus each Pause..Resume (an unmatched Pause
# runs to the end). The cuts go from both picture and voice.
kept, on_air = [], w0
for w, mark in sorted(marks):
    if mark == "recpause" and on_air is not None:
        kept.append((on_air, w))
        on_air = None
    elif mark == "recresume" and on_air is None:
        on_air = w
if on_air is not None:
    kept.append((on_air, w1))
kept = [(max(a, w0), min(b, w1)) for a, b in kept]
kept = [(a, b) for a, b in kept if b - a >= 1.0 / FPS]
if not kept:
    sys.exit("nothing on the air in this take")
cuts = [(b, a2) for (_, b), (a2, _) in zip(kept, kept[1:])]
if kept[-1][1] < w1:
    cuts.append((kept[-1][1], w1))


def path_for(a, b):
    # The state at a (the page logs "start" at the Record click, a moment
    # before the recorder's first sample; a playing row moves on by the
    # elapsed time), then every row up to b.
    before = [r for r in rows if r[0] <= a]
    path = [r for r in rows if a < r[0] < b]
    if before:
        wb, pb, plb = before[-1]
        path.insert(0, (a, pb + (a - wb if plb else 0), plb))
    return path + [(b, None, None)]


def pieces_for(path):
    # (kind, src_pos, duration). A playing row plays from its position for the
    # wall-clock gap to the next row: a pause that ends a drag-while-playing
    # carries the drag's first target, not the last played position, so the
    # stretch's length comes from wall time. Jumps are their own seeked rows.
    pieces = []
    for (wa, pa, pla), (wb, pb, _) in zip(path, path[1:]):
        dt = wb - wa
        if dt <= 0 or pa is None:
            continue
        if pla:
            if pieces and pieces[-1][0] == "play" and abs(pieces[-1][1] + pieces[-1][2] - pa) < 0.35:
                pieces[-1] = ("play", pieces[-1][1], pieces[-1][2] + dt)
            else:
                pieces.append(("play", pa, dt))
        else:
            if pieces and pieces[-1][0] == "hold" and abs(pieces[-1][1] - pa) < 0.05:
                pieces[-1] = ("hold", pa, pieces[-1][2] + dt)
            else:
                pieces.append(("hold", pa, dt))
    return [p for p in pieces if p[2] >= 1.0 / FPS]


# Picture: each kept interval's pieces, in order (no merging across a cut).
path, pieces = [], []
for a, b in kept:
    p = path_for(a, b)
    if len(p) < 2 or p[0][1] is None:
        sys.exit("no player events for this take")
    path += p[:-1]
    pieces += pieces_for(p)

# Voice: the same intervals sliced from audio.pcm, with 10ms fades at each
# join so a splice doesn't click.
FADE = RATE // 100
voice = array.array("h")
with open(os.path.join(D, "audio.pcm"), "rb") as fh:
    for i, (a, b) in enumerate(kept):
        fh.seek(int((a - t0) * RATE) * 2)
        chunk = array.array("h")
        chunk.frombytes(fh.read(int((b - a) * RATE) * 2))
        n_s = len(chunk)
        for k in range(min(FADE, n_s)):
            if i > 0:
                chunk[k] = int(chunk[k] * k / FADE)
            if i < len(kept) - 1:
                chunk[n_s - 1 - k] = int(chunk[n_s - 1 - k] * k / FADE)
        voice.extend(chunk)
wav = os.path.join(HERE, "takes", f"{name}.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(voice.tobytes())

# Render each piece, then join, then lay the voice over.
parts = []
for i, (kind, pos, dur) in enumerate(pieces):
    out = os.path.join(HERE, "narrated", "parts", f"{name}-{i:03d}.mp4")
    if kind == "play":
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{pos:.3f}", "-i", SRC, "-t", f"{dur:.3f}", *ENC, out]
    else:
        # Take the first frame at pos, then hold it for dur. (-frames:v would
        # cap the OUTPUT at one frame, collapsing every pause.)
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{pos:.3f}", "-i", SRC,
               "-vf", f"trim=end_frame=1,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={dur:.3f}",
               *ENC, out]
    subprocess.run(cmd, check=True)
    parts.append(out)
lst = os.path.join(HERE, "narrated", "parts", f"{name}.txt")
with open(lst, "w") as fh:
    fh.writelines(f"file '{p}'\n" for p in parts)
silent = os.path.join(HERE, "narrated", "parts", f"{name}-video.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", silent],
               check=True)

pre = "highpass=f=80,acompressor=threshold=-24dB:ratio=3:attack=5:release=120"
m1 = subprocess.run(["ffmpeg", "-v", "info", "-i", wav, "-af",
                     f"{pre},loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                    capture_output=True, text=True).stderr
ln = json.loads(m1[m1.rindex("{"):m1.rindex("}") + 1])
level = (f"{pre},loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:"
         f"measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}:offset={ln['target_offset']}:linear=true")
if "inf" in ln["input_i"]:
    level = "anull"  # a silent take has no loudness to normalize (measured_I is -inf)
final = os.path.join(HERE, "narrated", f"{name}.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", wav, "-filter_complex",
                f"[1:a]{level},afade=t=in:d=0.1,aresample=48000[a]", "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", final], check=True)


def tc(s):
    return f"{int(s // 60)}:{s % 60:05.2f}"


on_air_s = sum(b - a for a, b in kept)
src_start, src_end = path[0][1], pieces[-1][1] + (pieces[-1][2] if pieces[-1][0] == "play" else 0)
meta = {"take": name, "session": os.path.relpath(D, HERE), "source": os.path.basename(SRC),
        "wall_start": w0, "wall_end": w1, "duration": round(on_air_s, 2),
        "cuts": [(round(a - w0, 2), round(b - w0, 2)) for a, b in cuts],
        "src_start": tc(src_start), "src_end": tc(src_end),
        "pieces": [{"kind": k, "src": tc(p), "dur": round(d, 2)} for k, p, d in pieces],
        "path": [(round(w - w0, 2), p, pl) for w, p, pl in path]}
json.dump(meta, open(os.path.join(HERE, "takes", f"{name}.json"), "w"), indent=1)
plays = sum(d for k, _, d in pieces if k == "play")
print(f"{name}: {on_air_s:.1f}s on the air ({len(cuts)} cuts), source {tc(src_start)} -> {tc(src_end)}, "
      f"{len(pieces)} pieces ({plays:.1f}s playing, {on_air_s - plays:.1f}s held) -> {final}")
