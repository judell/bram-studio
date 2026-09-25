#!/usr/bin/env python3
"""Save and render one performance take.

    python3 render.py sessions/<stamp> <go_end_s> <stop_start_s> <source.mp4> \
        --events <events.json>

Descended from an earlier take.py (QuickTime + a polled playhead log). The
playhead path comes from the studio's MediaPlayer events (start/play/pause/
seeked/ended, each with currentTime and wallMs). It works in the repo's work/
folder: sessions/<stamp>/, takes/ and narrated/ live there, and the take is
named take-<stamp> after its session, so names never repeat.

The take runs from just after "go" to just before "stop" (GAP trimmed on
each side), minus any stretch between a recpause and a recresume event (the
app's Pause/Resume): those are cut from both picture and voice. It saves
takes/take-<stamp>.wav (the voice), takes/take-<stamp>.json
(wall times, source start/stop timecodes, the playhead path), and renders
narrated/take-<stamp>.mp4 by replaying the path against the source:
  playing          -> that source range at 1x
  paused           -> the frame held
  jump or scrub    -> a cut (short holds at each scrub position)
Voice is leveled like mix.py (gentle compression, two-pass loudnorm to
-16 LUFS, -1.5 dBTP).
"""
import array
import math
import json
import os
import subprocess
import sys
import wave

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
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
name = "take-" + os.path.basename(os.path.normpath(D))
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


# Ink (PointerLayer, xmlui-org/xmlui#3919): strokes and pointer samples from the
# take's events, in 0-1 picture coordinates, drawn as a transparent layer over
# the picture in take time. Frames without ink point at one shared blank PNG.
INK_FADE_S = 1.5


def take_time(w):
    # A wall-clock moment's position in the take, or None inside a cut.
    t = 0.0
    for a, b in kept:
        if w < a:
            return None
        if w <= b:
            return t + (w - a)
        t += b - a
    return None


def rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(255 * alpha))


strokes, shapes, pointer = [], [], []
for e in json.load(open(EVENTS)):
    if e["event"] == "stroke":
        pts = [(take_time(e["wallMs"] / 1000.0 + p["t"] / 1000.0), p["x"], p["y"]) for p in e["points"]]
        pts = [p for p in pts if p[0] is not None]
        if pts:
            strokes.append({"pts": pts, "end": pts[-1][0], "color": e.get("color", "#ff3b30"),
                            "width": e.get("width", 4)})
    elif e["event"] == "shape":
        # Tools (build 2): geometry, not sampled points. Grow in over the drag's
        # duration, then hold until release and fade like strokes.
        start = take_time(e["wallMs"] / 1000.0)
        if start is not None:
            shapes.append({"tool": e["tool"], "box": (e["x1"], e["y1"], e["x2"], e["y2"]), "start": start,
                           "end": start + e.get("durationMs", 0) / 1000.0,
                           "color": e.get("color", "#ff3b30"), "width": e.get("width", 4)})
    elif e["event"] == "pointer":
        tt = take_time(e["wallMs"] / 1000.0)
        if tt is not None:
            pointer.append((tt, e["x"], e["y"]))
pointer.sort()



def draw_shape(draw, sh, grow, color, w, W, H):
    # grow runs 0..1 over the drag; the shape is drawn up to that fraction.
    x1, y1, x2, y2 = sh["box"]
    X1, Y1 = x1 * W, y1 * H
    X2, Y2 = X1 + (x2 * W - X1) * grow, Y1 + (y2 * H - Y1) * grow
    if sh["tool"] in ("line", "arrow"):
        draw.line([(X1, Y1), (X2, Y2)], fill=color, width=w)
        if sh["tool"] == "arrow" and (X2, Y2) != (X1, Y1):
            ang, head = math.atan2(Y2 - Y1, X2 - X1), 5 * w
            for side in (-1, 1):
                a = ang + math.pi + side * math.radians(28)
                draw.line([(X2, Y2), (X2 + head * math.cos(a), Y2 + head * math.sin(a))], fill=color, width=w)
    elif sh["tool"] in ("rect", "ellipse"):
        box = (min(X1, X2), min(Y1, Y2), max(X1, X2), max(Y1, Y2))
        if box[2] - box[0] >= 1 and box[3] - box[1] >= 1:
            (draw.rectangle if sh["tool"] == "rect" else draw.ellipse)(box, outline=color, width=w)
    elif sh["tool"] == "pointer":
        r = round(14 * W / 1200.0)
        draw.ellipse((X1 - r, Y1 - r, X1 + r, Y1 + r), outline=color, width=w)


def draw_ink_frame(i):
    # One ink frame: strokes and shapes visible at take time i/FPS, or a link to
    # the shared blank. Runs in worker processes (forked, so globals are shared).
    from PIL import Image, ImageDraw
    T = i / FPS
    frame = os.path.join(inkdir, f"{i:06d}.png")
    if os.path.lexists(frame):
        os.remove(frame)
    img, draw = None, None
    for st in strokes:
        if st["pts"][0][0] > T or T > st["end"] + INK_FADE_S:
            continue
        alpha = 1.0 if T <= st["end"] else 1.0 - (T - st["end"]) / INK_FADE_S
        xy = [(x * W, y * H) for tt, x, y in st["pts"] if tt <= T]
        if not xy:
            continue
        if img is None:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0)); draw = ImageDraw.Draw(img)
        w = max(2, round(st["width"] * scale))
        color = rgba(st["color"], alpha)
        if len(xy) > 1:
            draw.line(xy, fill=color, width=w, joint="curve")
        for x, y in (xy[0], xy[-1]):
            draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)
    for sh in shapes:
        if sh["start"] > T or T > sh["end"] + INK_FADE_S:
            continue
        span = sh["end"] - sh["start"]
        grow = 1.0 if sh["tool"] == "pointer" or span <= 0 else min(1.0, (T - sh["start"]) / span)
        alpha = 1.0 if T <= sh["end"] else 1.0 - (T - sh["end"]) / INK_FADE_S
        if img is None:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0)); draw = ImageDraw.Draw(img)
        draw_shape(draw, sh, grow, rgba(sh["color"], alpha), max(2, round(sh["width"] * scale)), W, H)
    if img is None:
        os.symlink(blank, frame)
        return 0
    img.save(frame, compress_level=1)
    return 1


# Pointer samples stay in the event log but aren't drawn: a passive dot on most
# frames cost most of the render time, and the Point tool covers pointing.
if strokes or shapes:
    import multiprocessing
    from PIL import Image
    W, H = (int(v) for v in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of",
         "csv=p=0", silent], capture_output=True, text=True, check=True).stdout.strip().split(","))
    scale = W / 1200.0  # the live layer draws `width` CSS px over a ~1200px-wide player
    inkdir = os.path.join(HERE, "narrated", "parts", f"{name}-ink")
    os.makedirs(inkdir, exist_ok=True)
    blank = os.path.join(inkdir, "blank.png")
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank, compress_level=1)
    vdur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 silent], capture_output=True, text=True, check=True).stdout.strip())
    with multiprocessing.get_context("fork").Pool() as pool:
        drawn = sum(pool.map(draw_ink_frame, range(int(vdur * FPS)), chunksize=16))
    inked = os.path.join(HERE, "narrated", "parts", f"{name}-video-ink.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-framerate", str(FPS),
                    "-i", os.path.join(inkdir, "%06d.png"), "-filter_complex",
                    "[0:v][1:v]overlay=0:0:shortest=1:format=auto", *ENC, inked], check=True)
    silent = inked
    print(f"ink: {len(strokes)} strokes, {len(shapes)} shapes, {drawn} frames drawn "
          f"({len(pointer)} pointer samples logged, not drawn)")

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
        "path": [(round(w - w0, 2), p, pl) for w, p, pl in path],
        "ink": {"strokes": len(strokes), "shapes": len(shapes), "pointerSamples": len(pointer)}}
json.dump(meta, open(os.path.join(HERE, "takes", f"{name}.json"), "w"), indent=1)
plays = sum(d for k, _, d in pieces if k == "play")
print(f"{name}: {on_air_s:.1f}s on the air ({len(cuts)} cuts), source {tc(src_start)} -> {tc(src_end)}, "
      f"{len(pieces)} pieces ({plays:.1f}s playing, {on_air_s - plays:.1f}s held) -> {final}")
