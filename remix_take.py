#!/usr/bin/env python3
"""Re-mix a take's sound after the source-floor fix, without re-recording.

    python3 remix_take.py <take stamp> [...]     e.g. 20260928-155711
    python3 remix_take.py --dry-run <take stamp> [...]

Takes rendered before render.py's SOURCE_FLOOR leveled every source track to
-16 LUFS, which turned a source with no program (only its room's rumble) up
by as much as 30 dB. This redoes record.sh's mix step for such a take from
the tracks still in work/takes/ (the voice file and <take>-source.wav), with
the source mixed as recorded.

It replaces only the audio stream of media/<take>-raw.mp4 and of its clean
copy in media/.clean/; the video is copied as-is, so burned-in callouts stay.
The old files go to media/.trash/ first (<name>-before-remix-<stamp>). The
take's JSON gets sourceLevel "anull", and its row's url a new ?v= so players
reload it. A take whose source is at or above the floor is left alone.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK, MEDIA = os.path.join(HERE, "work"), os.path.join(HERE, "media")
SOURCE_FLOOR = -35.0  # render.py's


def loudness(path):
    err = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-af",
                          "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    i = json.loads(err[err.rindex("{"):err.rindex("}") + 1])["input_i"]
    return float("-inf") if "inf" in i else float(i)


def remix(stamp, dry_run):
    take = f"take-{stamp}"
    meta_path = os.path.join(WORK, "takes", f"{take}.json")
    src_wav = os.path.join(WORK, "takes", f"{take}-source.wav")
    if not os.path.exists(meta_path) or not os.path.exists(src_wav):
        return print(f"{take}: no JSON or no source track in work/takes/; skipped")
    meta = json.load(open(meta_path))
    if meta.get("cutsAfterRender"):
        # serve_media.py's /takes/cut shortens the take's MP4s, not these tracks.
        return print(f"{take}: has been cut since it was rendered ({meta['cutsAfterRender']}); "
                     "its tracks in work/takes/ no longer line up, so not re-mixed. Undo the cut first.")
    voice = os.path.join(WORK, "takes", meta.get("voiceFile") or f"{take}.wav")
    vlevel = meta.get("voiceLevel") or "anull"
    level = loudness(src_wav)
    if level >= SOURCE_FLOOR:
        return print(f"{take}: source at {level:.1f} LUFS has program (floor {SOURCE_FLOOR}); left alone")
    if meta.get("sourceLevel") == "anull":
        return print(f"{take}: source at {level:.1f} LUFS, already mixed without boost; left alone")
    targets = [p for p in (os.path.join(MEDIA, f"{take}-raw.mp4"), os.path.join(MEDIA, ".clean", f"{take}-raw.mp4"))
               if os.path.exists(p)]
    print(f"{take}: source at {level:.1f} LUFS, below {SOURCE_FLOOR}: re-mixing without boost "
          f"({', '.join(os.path.relpath(p, HERE) for p in targets)})")
    if dry_run or not targets:
        return
    # record.sh's mix, with the source track as recorded (anull).
    mix = (f"[1:a]{vlevel},aresample=48000,afade=t=in:d=0.1,pan=stereo|c0=c0|c1=c0[v];"
           "[2:a]anull,aresample=48000[s];[v][s]amix=inputs=2:normalize=0[a]")
    trash = os.path.join(MEDIA, ".trash")
    os.makedirs(trash, exist_ok=True)
    when = time.strftime("%Y%m%d-%H%M%S")
    for path in targets:
        tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)[:-4]}.remix.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-i", voice, "-i", src_wav,
                        "-filter_complex", mix, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "160k", "-shortest", tmp], check=True)
        kind = "clean-" if os.sep + ".clean" + os.sep in path else ""
        shutil.move(path, os.path.join(trash, f"{take}-raw-{kind}before-remix-{when}.mp4"))
        os.replace(tmp, path)
    meta["sourceLevel"] = "anull"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=1)
    db = sqlite3.connect(os.path.join(HERE, "studio.db"))
    db.execute("UPDATE takes SET url = substr(url, 1, instr(url || '?', '?') - 1) || '?v=' || ? WHERE file = ?",
               (int(time.time()), f"{take}-raw.mp4"))
    db.commit()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        sys.exit(__doc__.split("\n\n")[1])
    for stamp in args:
        remix(stamp, "--dry-run" in sys.argv)
