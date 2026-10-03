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
reload it. A take whose source is at or above the floor is left alone. A take
cut or paused since it was rendered is refused (its tracks no longer line up
with it), with a pointer to --regate, which replays those edits.

    python3 remix_take.py --regate [--dry-run] <take stamp> [...]

Takes rendered before the voice gate kept the mic out where the source
speaks (voicegate.py) have the source's own voice twice: in the source track
and in the mic's recording of the speakers. --regate redoes such a take's
voice from its raw mic slice (denoise, gate, level), levels its source track
again (one gain, where older takes had loudnorm riding it) and redoes the
mix, with record.sh's limiter. The audio is replaced the same way. A take cut or paused
since it was rendered has those edits (cutsAfterRender in its JSON) replayed
on the new audio; one narrated over is refused, since that audio exists only
in the take. An Undo still on offer for the take's last edit no longer
applies afterwards (the file has changed).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import wave

from studio_paths import DB, MEDIA, PROJECT, WORK
from voicegate import LIMITER, SOURCE_FLOOR, VOICE_PRE, gate_voice, level_filter

FMT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


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
        # --regate replays those edits, without the undo history (which a
        # Projects page clean-up may have cleared), so the refusal points there.
        return print(f"{take}: has been cut or paused since it was rendered ({meta['cutsAfterRender']}), so its "
                     "tracks in work/takes/ no longer line up and it isn't re-mixed this way. --regate rebuilds "
                     "its sound from those tracks and replays the edits (unless it was narrated over): "
                     f"python3 remix_take.py --regate {stamp} (add --dry-run to see first).")
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
          f"({', '.join(os.path.relpath(p, PROJECT) for p in targets)})")
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
    bump_url(take)


def bump_url(take):
    db = sqlite3.connect(DB)
    db.execute("UPDATE takes SET url = substr(url, 1, instr(url || '?', '?') - 1) || '?v=' || ? WHERE file = ?",
               (int(time.time()), f"{take}-raw.mp4"))
    db.commit()


def replay_edits(edits, dur):
    # The filter steps that make serve_media.py's cuts and pauses (its
    # cut_take and pause_take, audio side) again on [m0], a mix `dur` seconds
    # long, in the order they were made. Returns (steps, last label), or None
    # for an edit that can't be replayed.
    steps, n = [], 0
    for e in edits:
        kind, rec = ("cut", e) if isinstance(e, list) else next(iter(e.items()))
        cur, n = f"[m{n}]", n + 1
        if kind == "cut":
            a, b = max(0.0, rec[0]), min(dur, rec[1])
            head, tail = a > 0.02, dur - b > 0.02
            if head and tail:
                steps.append(f"{cur}asplit[h{n}][t{n}];"
                             f"[h{n}]atrim=start=0:end={a:.3f},asetpts=PTS-STARTPTS,"
                             f"afade=t=out:st={a - 0.01:.3f}:d=0.01[x{n}];"
                             f"[t{n}]atrim=start={b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.01[y{n}];"
                             f"[x{n}][y{n}]concat=n=2:v=0:a=1[m{n}]")
            elif head:
                steps.append(f"{cur}atrim=start=0:end={a:.3f},asetpts=PTS-STARTPTS[m{n}]")
            else:
                steps.append(f"{cur}atrim=start={b:.3f},asetpts=PTS-STARTPTS[m{n}]")
            dur -= b - a
        elif kind == "pause":
            at, secs = min(max(0.0, rec[0]), dur), rec[1]
            quiet = f"anullsrc=r=48000:cl=stereo,atrim=duration={secs:.3f},{FMT}[q{n}];"
            if at < 0.02:
                steps.append(f"{quiet}{cur}afade=t=in:d=0.01[y{n}];[q{n}][y{n}]concat=n=2:v=0:a=1[m{n}]")
            elif at > dur - 0.02:
                steps.append(f"{quiet}{cur}afade=t=out:st={max(0, dur - 0.01):.3f}:d=0.01[x{n}];"
                             f"[x{n}][q{n}]concat=n=2:v=0:a=1[m{n}]")
            else:
                steps.append(f"{quiet}{cur}asplit[h{n}][t{n}];"
                             f"[h{n}]atrim=start=0:end={at:.3f},asetpts=PTS-STARTPTS,"
                             f"afade=t=out:st={max(0, at - 0.01):.3f}:d=0.01[x{n}];"
                             f"[t{n}]atrim=start={at:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.01[y{n}];"
                             f"[x{n}][q{n}][y{n}]concat=n=3:v=0:a=1[m{n}]")
            dur += secs
        else:
            return None
    return steps, f"[m{n}]"


def regate(stamp, dry_run):
    take = f"take-{stamp}"
    meta_path = os.path.join(WORK, "takes", f"{take}.json")
    raw = os.path.join(WORK, "takes", f"{take}.wav")
    src_wav = os.path.join(WORK, "takes", f"{take}-source.wav")
    if not all(os.path.exists(p) for p in (meta_path, raw, src_wav)):
        return print(f"{take}: no JSON, raw voice or source track in work/takes/; skipped")
    meta = json.load(open(meta_path))
    with wave.open(raw) as w:
        dur = w.getnframes() / w.getframerate()
    edits = replay_edits(meta.get("cutsAfterRender") or [], dur)
    if edits is None:
        return print(f"{take}: has an edit that can't be replayed ({meta['cutsAfterRender']}); not re-gated")
    targets = [p for p in (os.path.join(MEDIA, f"{take}-raw.mp4"), os.path.join(MEDIA, ".clean", f"{take}-raw.mp4"))
               if os.path.exists(p)]
    if not targets:
        return print(f"{take}: no take file in media/; skipped")
    # The voice again as render.py makes it: denoise, gate, level.
    clean = os.path.join(WORK, "takes", f"{take}-voice-clean.wav")
    tmp_clean = clean[:-4] + ".regate.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, "-af", "anlmdn=s=0.0005:p=0.002:r=0.006", tmp_clean],
                   check=True)
    print(f"{take}: ", end="", flush=True)
    if not gate_voice(tmp_clean, src_wav, tmp_clean):
        os.remove(tmp_clean)
        return
    vlevel = level_filter(tmp_clean, VOICE_PRE)
    # The source's level again too: a take's stored sourceLevel may be the
    # old loudnorm filter, which rode the gain (voicegate.level_filter).
    slevel = level_filter(src_wav, floor=SOURCE_FLOOR)
    steps, out = edits
    print(f"{take}: voice {'silent' if vlevel == 'anull' else 'leveled'}, {len(steps)} edit"
          f"{'' if len(steps) == 1 else 's'} to replay ({', '.join(os.path.relpath(p, PROJECT) for p in targets)})")
    if dry_run:
        os.remove(tmp_clean)
        return
    # record.sh's leveled mix, then the take's later edits.
    mix = ";".join([f"[1:a]{vlevel},aresample=48000,afade=t=in:d=0.1,pan=stereo|c0=c0|c1=c0[v];"
                    f"[2:a]{slevel},aresample=48000[s];[v][s]amix=inputs=2:normalize=0,{LIMITER},{FMT}[m0]", *steps])
    trash = os.path.join(MEDIA, ".trash")
    os.makedirs(trash, exist_ok=True)
    when = time.strftime("%Y%m%d-%H%M%S")
    tmps = []
    for path in targets:
        tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)[:-4]}.remix.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-i", tmp_clean, "-i", src_wav,
                        "-filter_complex", mix, "-map", "0:v", "-map", out, "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest", tmp], check=True)
        tmps.append(tmp)
    for path, tmp in zip(targets, tmps):
        kind = "clean-" if os.sep + ".clean" + os.sep in path else ""
        shutil.move(path, os.path.join(trash, f"{take}-raw-{kind}before-remix-{when}.mp4"))
        os.replace(tmp, path)
    os.replace(tmp_clean, clean)
    meta.update(voiceFile=os.path.basename(clean), voiceLevel=vlevel, sourceLevel=slevel)
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=1)
    bump_url(take)


if __name__ == "__main__":
    flags = ("--dry-run", "--regate")
    args = [a for a in sys.argv[1:] if a not in flags]
    if not args:
        sys.exit(__doc__.split("\n\n")[1])
    for stamp in args:
        (regate if "--regate" in sys.argv else remix)(stamp, "--dry-run" in sys.argv)
