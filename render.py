#!/usr/bin/env python3
"""Save and render one performance take.

    python3 render.py sessions/<stamp> <go_end_s> <stop_start_s> <source.mp4>

Copied from ~/Desktop/video-test/take.py so the source movie is an argument
(take.py hardcodes ~/Desktop/bram-sep-23.mp4), and so the playhead path uses
only rows logged for that movie. It still works in ~/Desktop/video-test:
session dirs, playhead.log, takes/ and narrated/ all live there.

The take runs from just after "go" to just before "stop" (GAP trimmed on
each side). It saves takes/perf-<n>.wav (the voice), takes/perf-<n>.json
(wall times, source start/stop timecodes, the playhead path), and renders
narrated/perf-<n>.mp4 by replaying the path against the source:
  playing          -> that source range at 1x
  paused           -> the frame held
  jump or scrub    -> a cut (short holds at each scrub position)
Voice is leveled like mix.py (gentle compression, two-pass loudnorm to
-16 LUFS, -1.5 dBTP).
"""
import glob
import json
import os
import re
import subprocess
import sys
import wave

HERE = os.path.expanduser("~/Desktop/video-test")
LOG = os.path.join(HERE, "playhead.log")
RATE, GAP, FPS = 48000, 0.2, 25
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-tune", "stillimage",
       "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]

D, go_end, stop_start = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
SRC = os.path.realpath(sys.argv[4])
D = os.path.join(HERE, D)
t0 = float(open(os.path.join(D, "t0")).read())
a_s, b_s = go_end + GAP, stop_start - GAP
w0, w1 = t0 + a_s, t0 + b_s
n = 1 + max([int(re.search(r"perf-(\d+)\.json$", p).group(1))
             for p in glob.glob(os.path.join(HERE, "takes", "perf-*.json"))] or [0])
name = f"perf-{n}"
os.makedirs(os.path.join(HERE, "takes"), exist_ok=True)
os.makedirs(os.path.join(HERE, "narrated", "parts"), exist_ok=True)

# Voice.
wav = os.path.join(HERE, "takes", f"{name}.wav")
with open(os.path.join(D, "audio.pcm"), "rb") as fh:
    fh.seek(int(a_s * RATE) * 2)
    data = fh.read(int((b_s - a_s) * RATE) * 2)
with wave.open(wav, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(data)

# Playhead path: the state at w0, then every change up to w1.
rows = []
for line in open(LOG):
    f = line.rstrip("\n").split("\t")
    # playhead.js logs QuickTime's document name 4th; keep only this movie's rows.
    if len(f) >= 4 and f[1] != "error" and f[3] == os.path.basename(SRC):
        rows.append((int(f[0]) / 1000.0, float(f[1]), f[2] == "true"))
before = [r for r in rows if r[0] <= w0]
path = ([(w0, before[-1][1], before[-1][2])] if before else []) + [r for r in rows if w0 < r[0] < w1]
if not path:
    sys.exit("no playhead data for this take")
path.append((w1, None, None))

# Pieces: (kind, src_pos, duration). Contiguous play samples merge.
pieces = []
for (wa, pa, pla), (wb, pb, _) in zip(path, path[1:]):
    dt = wb - wa
    if dt <= 0:
        continue
    contiguous = pb is not None and abs((pb - pa) - dt) < 0.35
    if pla and (contiguous or pb is None):
        if pieces and pieces[-1][0] == "play" and abs(pieces[-1][1] + pieces[-1][2] - pa) < 0.35:
            pieces[-1] = ("play", pieces[-1][1], pieces[-1][2] + dt)
        else:
            pieces.append(("play", pa, dt))
    else:
        if pieces and pieces[-1][0] == "hold" and abs(pieces[-1][1] - pa) < 0.05:
            pieces[-1] = ("hold", pa, pieces[-1][2] + dt)
        else:
            pieces.append(("hold", pa, dt))
pieces = [p for p in pieces if p[2] >= 1.0 / FPS]

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
final = os.path.join(HERE, "narrated", f"{name}.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", wav, "-filter_complex",
                f"[1:a]{level},afade=t=in:d=0.1,aresample=48000[a]", "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", final], check=True)


def tc(s):
    return f"{int(s // 60)}:{s % 60:05.2f}"


src_start, src_end = path[0][1], [p for p in path if p[1] is not None][-1][1]
meta = {"take": name, "session": os.path.relpath(D, HERE), "source": os.path.basename(SRC),
        "wall_start": w0, "wall_end": w1, "duration": round(w1 - w0, 2),
        "src_start": tc(src_start), "src_end": tc(src_end),
        "pieces": [{"kind": k, "src": tc(p), "dur": round(d, 2)} for k, p, d in pieces],
        "path": [(round(w - w0, 2), p, pl) for w, p, pl in path[:-1]]}
json.dump(meta, open(os.path.join(HERE, "takes", f"{name}.json"), "w"), indent=1)
plays = sum(d for k, _, d in pieces if k == "play")
print(f"{name}: {w1 - w0:.1f}s, source {tc(src_start)} -> {tc(src_end)}, {len(pieces)} pieces "
      f"({plays:.1f}s playing, {w1 - w0 - plays:.1f}s held) -> {final}")
