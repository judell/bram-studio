#!/usr/bin/env python3
"""Text over a take: captions and callouts, burned into its MP4.

    python3 overlay.py render <take id>
    python3 overlay.py preview <take id> <item.json> <out.png>

A take's text items live in studio.db's overlays table ({text, x1, y1, x2, y2,
t_in, t_out} in 0-1 picture coordinates and take seconds; kind 'caption' or
'callout'). The first time a take gets text, its MP4 is copied to
media/.clean/, and every render starts from that copy, so text can change or
go away without piling up. ffmpeg here has no drawtext/subtitles filters, so
the text is drawn with Pillow into one transparent frame per video frame, like
render.py's ink, and composited with ffmpeg's overlay. The take's url gets
?v=<time> so the player doesn't show a cached copy. Callouts are drawn with
their top-left at (x1, y1), wrapped to the box width; captions (kind
'caption', a band at the bottom) are laid out too, though nothing makes them
yet (the whisper captioning is set aside). Prints one JSON line:
{"id", "items", "seconds"}.
"""
import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")
FONT, FONT_INDEX = "/System/Library/Fonts/HelveticaNeue.ttc", 10  # Helvetica Neue Medium
FADE_S = 0.2
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-tune", "stillimage", "-pix_fmt", "yuv420p"]
SCHEMA = """CREATE TABLE IF NOT EXISTS overlays (
  id INTEGER PRIMARY KEY,
  take_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  x1 REAL, y1 REAL, x2 REAL, y2 REAL,
  t_in REAL NOT NULL,
  t_out REAL NOT NULL,
  tail TEXT,
  shape TEXT
)"""
# The eight places a callout's tail can come off its box.
TAILS = ("n", "ne", "e", "se", "s", "sw", "w", "nw")
# Shapes on a take (kind 'shape'), drawn like recording ink: the box for rect
# and ellipse, tail then tip for arrow, the tip in (x1, y1) for pointer.
SHAPES = ("rect", "ellipse", "arrow", "pointer")
INK_COLOR = "#ff3b30"


# A take's text and shapes as one string, in id order: stored in
# takes.overlay_applied when they're burned in, and computed the same way by
# the page (Main.xmlui's applyState, which must keep this exact text) to tell
# whether Apply has anything to do. char(58) ':' and char(124) '|' keep quotes
# out of it.
APPLIED_SQL = ("SELECT group_concat(id || char(58) || text || char(58) || x1 || char(58) || y1 || char(58) || x2 "
               "|| char(58) || y2 || char(58) || t_in || char(58) || t_out || char(58) || ifnull(tail, char(45)) "
               "|| char(58) || ifnull(shape, char(45)), char(124)) "
               "FROM (SELECT * FROM overlays WHERE take_id = ? ORDER BY id)")


def ensure_schema(db):
    # The overlays table, and columns added since it was first made.
    db.execute(SCHEMA)
    columns = [c[1] for c in db.execute("PRAGMA table_info(overlays)")]
    for col in ("tail", "shape"):
        if col not in columns:
            db.execute(f"ALTER TABLE overlays ADD COLUMN {col} TEXT")
    if "overlay_applied" not in [c[1] for c in db.execute("PRAGMA table_info(takes)")]:
        db.execute("ALTER TABLE takes ADD COLUMN overlay_applied TEXT")


def draw_shape(draw, sh, grow, color, w, W, H):
    # Recording ink's shapes (render.py) and shapes on a take (kind 'shape'):
    # sh = {"tool", "box": (x1, y1, x2, y2) in 0-1 picture coordinates,
    # "pointerShape"}. grow runs 0..1 over a recorded drag; the shape is drawn
    # up to that fraction.
    import math
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
    elif sh["tool"] == "pointer" and sh.get("pointerShape") == "ring":
        r = round(14 * W / 1200.0)
        draw.ellipse((X1 - r, Y1 - r, X1 + r, Y1 + r), outline=color, width=w)
    elif sh["tool"] == "pointer":
        # A fat block arrow, tip on the click, pointing up and to the right at
        # 45 degrees: PointerLayer's pointerShape="arrow" (#3919), the same
        # polygon at the same size (6% of the picture width).
        L = 0.06 * W
        hl, hw, sw = 0.45 * L, 0.32 * L, 0.12 * L
        # Along the arrow (u, tip at 0) and across it (v).
        outline = [(0, 0), (-hl, hw), (-hl, sw), (-L, sw), (-L, -sw), (-hl, -sw), (-hl, -hw)]
        c, s = math.cos(math.radians(-45)), math.sin(math.radians(-45))
        pts = [(X1 + u * c - v * s, Y1 + u * s + v * c) for u, v in outline]
        draw.polygon(pts, fill=color, outline=(255, 255, 255, color[3]), width=max(2, round(2 * W / 1200.0)))


def probe(path, entries, stream=None):
    args = ["ffprobe", "-v", "error", *(["-select_streams", stream] if stream else []),
            "-show_entries", entries, "-of", "csv=p=0", path]
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def wrap(draw, font, text, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def fit(draw, text, size, maxw):
    # The text wrapped at one font size: its lines and the box around them.
    from PIL import ImageFont
    font = ImageFont.truetype(FONT, size, index=FONT_INDEX)
    pad, gap = round(size * 0.45), round(size * 0.25)
    lines = wrap(draw, font, text, maxw - 2 * pad)
    lw = max(draw.textlength(ln, font=font) for ln in lines)
    asc, desc = font.getmetrics()
    return {"font": font, "lines": lines, "lw": lw, "pad": pad, "gap": gap, "line_h": asc + desc,
            "bw": lw + 2 * pad, "bh": len(lines) * (asc + desc) + (len(lines) - 1) * gap + 2 * pad,
            "radius": round(size * 0.3)}


def layout(item, W, H):
    # Where an item's box and lines go. Captions: centered in a band at the
    # bottom (x1..x2 wide, bottom edge at y2). Callouts: top-left at (x1, y1),
    # at the largest size (up to 3x the caption size, so text grows with a
    # bigger box) whose wrapped text fits the box drawn for it, width and
    # height; words are never split. A box too small to mean anything (a
    # click, not a drag) gets the caption size.
    from PIL import Image, ImageDraw
    if item["kind"] == "shape":
        # Shapes have no text: draw_shape's arguments, with recording ink's
        # line width (4 px over a ~1200-px-wide player, scaled to the take).
        return {"shape": {"tool": item.get("shape"), "box": (item["x1"], item["y1"], item["x2"], item["y2"]),
                          "pointerShape": "arrow"}, "W": W, "H": H, "w": max(2, round(4 * W / 1200.0))}
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    top = round(0.042 * min(W, H))
    bw_max, bh_max = (item["x2"] - item["x1"]) * W, (item["y2"] - item["y1"]) * H
    if item["kind"] == "caption":
        L = fit(draw, item["text"], top, bw_max)
    elif bw_max < 0.05 * W or bh_max < 0.03 * H:
        L = fit(draw, item["text"], top, 0.4 * W)
    else:
        # Binary search for the largest size that fits (the wrapped text only
        # grows with the size); the smallest, 10, if nothing does.
        lo, hi = 10, 3 * top
        L = fit(draw, item["text"], lo, bw_max)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            M = fit(draw, item["text"], mid, bw_max)
            if M["bw"] <= bw_max and M["bh"] <= bh_max:
                lo, L = mid, M
            else:
                hi = mid - 1
    if item["kind"] == "caption":
        left, y = ((item["x1"] + item["x2"]) / 2) * W - L["bw"] / 2, item["y2"] * H - L["bh"]
    else:
        left, y = item["x1"] * W, item["y1"] * H
    left, y = max(0, min(left, W - L["bw"])), max(0, min(y, H - L["bh"]))
    L.update(box=(left, y, left + L["bw"], y + L["bh"]), center=item["kind"] == "caption",
             tail=item.get("tail") if item.get("tail") in TAILS else None)
    return L


def tail_points(L):
    # A speech-bubble tail: a triangle whose base sits inside the box at one of
    # the eight compass points and whose tip points outward from there, about
    # three quarters of a line of text beyond the box. Corners point
    # diagonally, from just inside the rounded corner.
    import math
    left, top, right, bottom = L["box"]
    dx = {"w": -1, "nw": -1, "sw": -1, "e": 1, "ne": 1, "se": 1}.get(L["tail"], 0)
    dy = {"n": -1, "nw": -1, "ne": -1, "s": 1, "sw": 1, "se": 1}.get(L["tail"], 0)
    inset = L["radius"] * 1.2
    px = {-1: left + inset if dy else left, 0: (left + right) / 2, 1: right - inset if dy else right}[dx]
    py = {-1: top + inset if dx else top, 0: (top + bottom) / 2, 1: bottom - inset if dx else bottom}[dy]
    n = math.hypot(dx, dy)
    ux, uy = dx / n, dy / n
    length, half = 0.75 * L["line_h"] + inset, 0.35 * L["line_h"]
    return [(px - uy * half, py + ux * half), (px + uy * half, py - ux * half),
            (px + ux * length, py + uy * length)]


def paint(img, drawn):
    # Draw (layout, alpha) pairs onto an RGBA image.
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    for L, a in drawn:
        if "shape" in L:
            h = INK_COLOR.lstrip("#")
            color = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), round(255 * a))
            draw_shape(draw, L["shape"], 1.0, color, L["w"], L["W"], L["H"])
            continue
        fill = (0, 0, 0, round(165 * a))
        if L.get("tail"):
            draw.polygon(tail_points(L), fill=fill)
        draw.rounded_rectangle(L["box"], radius=L["radius"], fill=fill)
        y = L["box"][1] + L["pad"]
        for ln in L["lines"]:
            x = L["box"][0] + L["pad"]
            if L["center"]:
                x += (L["lw"] - draw.textlength(ln, font=L["font"])) / 2
            draw.text((x, y), ln, font=L["font"], fill=(255, 255, 255, round(255 * a)))
            y += L["line_h"] + L["gap"]


def alpha_at(item, t):
    if t < item["t_in"] or t > item["t_out"]:
        return 0.0
    return max(0.0, min(1.0, (t - item["t_in"]) / FADE_S, (item["t_out"] - t) / FADE_S))


def draw_frame(job):
    # One overlay frame: the given (item index, alpha) pairs drawn on a clear image.
    from PIL import Image
    path, state = job
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    paint(img, [(LAYOUTS[idx], a) for idx, a in state])
    img.save(path, compress_level=1)


FRAMES = {}  # (clean file, its mtime, time) -> RGBA frame, for repeated previews


def preview_image(db, take_id, item):
    # One still: the take's clean picture a moment after item's in point, with
    # item and any other text showing then, drawn exactly as render() draws it.
    # The frame is cached, so redrawing while a callout is dragged costs only
    # the drawing (serve_media.py calls this in-process).
    from PIL import Image
    row = db.execute("SELECT file FROM takes WHERE id = ?", (take_id,)).fetchone()
    if not row:
        raise LookupError(f"no take with id {take_id}")
    clean = os.path.join(MEDIA, ".clean", row[0])
    src = clean if os.path.exists(clean) else os.path.join(MEDIA, row[0])
    t = round(min(item["t_in"] + 0.3, (item["t_in"] + item["t_out"]) / 2), 2)
    key = (src, os.path.getmtime(src), t)
    if key not in FRAMES:
        with tempfile.TemporaryDirectory() as tmp:
            still = os.path.join(tmp, "f.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", still],
                           check=True)
            if len(FRAMES) >= 8:
                FRAMES.pop(next(iter(FRAMES)))
            FRAMES[key] = Image.open(still).convert("RGBA")
    img = FRAMES[key]
    W, H = img.size
    others = [dict(zip(("kind", "text", "x1", "y1", "x2", "y2", "t_in", "t_out", "tail", "shape"), r))
              for r in db.execute(
        "SELECT kind, text, x1, y1, x2, y2, t_in, t_out, tail, shape FROM overlays WHERE take_id = ? AND id != ?",
        (take_id, item.get("id") or -1))]
    drawn = [(layout(o, W, H), alpha_at(o, t)) for o in others if alpha_at(o, t) > 0]
    # Drawn on a clear layer and composited, as ffmpeg's overlay does in
    # render(): painting straight onto the frame would replace its pixels.
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    paint(layer, drawn + [(layout({**item, "kind": "callout"}, W, H), 1.0)])
    return Image.alpha_composite(img, layer).convert("RGB")


SIZES = {}  # (take file, its mtime) -> (W, H) of its picture


def dashed_frame(img, box, W, H):
    # A thin dashed outline of a callout's box (0-1 coordinates), dark under
    # light so it reads on any picture: the editor's resize frame, never
    # burned in.
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = box[0] * W, box[1] * H, box[2] * W - 1, box[3] * H - 1
    dash = max(4, round(0.008 * W))
    for (ax, ay), (bx, by) in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
        length = max(abs(bx - ax), abs(by - ay))
        for k in range(0, int(length), 2 * dash):
            f0, f1 = k / length, min(k + dash, length) / length
            seg = [(ax + (bx - ax) * f0, ay + (by - ay) * f0), (ax + (bx - ax) * f1, ay + (by - ay) * f1)]
            draw.line(seg, fill=(0, 0, 0, 200), width=3)
            draw.line(seg, fill=(255, 255, 255, 230), width=1)


def sprite(db, take_id, item, frame=False):
    # Just the callout (box, tail and text), or the shape when item["shape"]
    # names one, drawn as render() draws it, on a
    # clear layer the size of the picture, cropped to what was drawn. Returns
    # the image and its box in 0-1 picture coordinates, for PointerLayer's
    # anchors (xmlui-org/xmlui#3922) to place over the video. frame adds the
    # editor's dashed outline of the callout's box.
    from PIL import Image
    row = db.execute("SELECT file FROM takes WHERE id = ?", (take_id,)).fetchone()
    if not row:
        raise LookupError(f"no take with id {take_id}")
    src = os.path.join(MEDIA, row[0])
    key = (src, os.path.getmtime(src))
    if key not in SIZES:
        SIZES[key] = tuple(int(v) for v in probe(src, "stream=width,height", "v:0").split(","))
    W, H = SIZES[key]
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    kind = "shape" if item.get("shape") in SHAPES else "callout"
    paint(layer, [(layout({**item, "kind": kind}, W, H), 1.0)])
    if frame:
        dashed_frame(layer, (item["x1"], item["y1"], item["x2"], item["y2"]), W, H)
    # Nothing drawn (a shape too small to show): one clear pixel where it is.
    bbox = layer.getbbox()
    if not bbox:
        px, py = min(W - 1, max(0, round(item["x1"] * W))), min(H - 1, max(0, round(item["y1"] * H)))
        bbox = (px, py, px + 1, py + 1)
    x1, y1, x2, y2 = bbox
    return layer.crop((x1, y1, x2, y2)), {"x": x1 / W, "y": y1 / H, "width": (x2 - x1) / W, "height": (y2 - y1) / H}


def preview(db, take_id, item, out):
    try:
        img = preview_image(db, take_id, item)
    except LookupError as e:
        sys.exit(str(e))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    img.save(out)


def render(db, take_id):
    global W, H, LAYOUTS
    row = db.execute("SELECT file FROM takes WHERE id = ?", (take_id,)).fetchone()
    if not row:
        sys.exit(f"no take with id {take_id}")
    fname = row[0]
    take, clean = os.path.join(MEDIA, fname), os.path.join(MEDIA, ".clean", fname)
    if not os.path.exists(clean):
        os.makedirs(os.path.dirname(clean), exist_ok=True)
        shutil.copy2(take, clean)
    items = [dict(zip(("kind", "text", "x1", "y1", "x2", "y2", "t_in", "t_out", "tail", "shape"), r))
             for r in db.execute(
        "SELECT kind, text, x1, y1, x2, y2, t_in, t_out, tail, shape FROM overlays WHERE take_id = ? ORDER BY t_in",
        (take_id,))]
    tmp_out = take + ".overlay.mp4"
    if not items:
        shutil.copy2(clean, tmp_out)
    else:
        W, H = (int(v) for v in probe(clean, "stream=width,height", "v:0").split(","))
        num, den = (int(v) for v in probe(clean, "stream=r_frame_rate", "v:0").split("/"))
        fps = num / den
        frames = int(float(probe(clean, "format=duration")) * fps)
        LAYOUTS = [layout(it, W, H) for it in items]
        # Frames with the same items at the same opacity share one PNG: only
        # fades need their own, so steady captions cost one image each.
        work = tempfile.mkdtemp(prefix="overlay-", dir=os.path.join(HERE, "work"))
        drawn, jobs = {}, []
        for i in range(frames):
            t = i / fps
            state = tuple((k, round(a, 2)) for k, it in enumerate(items) if (a := alpha_at(it, t)) > 0)
            if state not in drawn:
                drawn[state] = os.path.join(work, f"s{len(drawn):05d}.png")
                jobs.append((drawn[state], state))
            os.symlink(drawn[state], os.path.join(work, f"{i:06d}.png"))
        with multiprocessing.get_context("fork").Pool() as pool:
            pool.map(draw_frame, jobs, chunksize=4)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", clean, "-framerate", f"{num}/{den}",
                        "-i", os.path.join(work, "%06d.png"), "-filter_complex",
                        "[0:v][1:v]overlay=0:0:shortest=1:format=auto[v]", "-map", "[v]", "-map", "0:a?",
                        *ENC, "-c:a", "copy", tmp_out], check=True)
        shutil.rmtree(work)
    os.replace(tmp_out, take)
    db.execute(f"UPDATE takes SET url = ?, overlay_applied = ({APPLIED_SQL}) WHERE id = ?",
               (f"http://127.0.0.1:8765/{fname}?v={int(time.time())}", take_id, take_id))
    db.commit()
    return items


def main():
    cmd, take_id = sys.argv[1], int(sys.argv[2])
    t0 = time.time()
    db = sqlite3.connect(os.path.join(HERE, "studio.db"))
    ensure_schema(db)
    if cmd == "preview":
        preview(db, take_id, json.load(open(sys.argv[3])), sys.argv[4])
        print(json.dumps({"id": take_id, "preview": sys.argv[4], "seconds": round(time.time() - t0, 1)}))
        return
    if cmd != "render":
        sys.exit(f"unknown command {cmd}")
    items = render(db, take_id)
    print(json.dumps({"id": take_id, "items": len(items), "seconds": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()
