#!/usr/bin/env python3
"""Level the audio of an edited export: cut rumble, even out loudness.

    python3 level_edit.py <edited.mp4> [out.mp4]

For a video made from exported takes and then cut, with pauses inserted, in
another editor. Its speech comes from sources recorded at different levels,
and its pauses carry low-frequency room rumble. Writes <name>-leveled.mp4
beside the input by default. The video stream is copied untouched; the audio
is re-encoded (AAC, 48 kHz).

  1. High-pass at HIGHPASS Hz, 4-pole. The rumble sits below ~120 Hz, where
     hiss denoisers (anlmdn, afftdn) don't reach: they took <1 dB off the
     pauses of the first edit; this takes ~5.5 dB off them and ~0.4 dB off
     the speech.
  2. A slow gain rider. Every STEP s it averages the momentary loudness
     (EBU R128) of speech frames (louder than SPEECH) within +/-WINDOW s and
     sets the gain that brings them to TARGET, within -MAX_CUT..+MAX_BOOST dB.
     loudnorm (alone or after dynaudnorm) left the first edit's loudness
     range at 16 LU; this brought it to ~9. Where the window holds too little
     speech it's a pause: the gain holds, but never lifts the room tone above
     PAUSE. The gain is smoothed over SMOOTH s so it doesn't pump.
  3. A limiter at LIMIT (sample peak), low enough that the AAC encoder's
     overshoot stays under -1 dBTP.

It prints integrated loudness, loudness range and true peak before and after.
Needs only ffmpeg and the Python standard library.
"""
import math
import os
import re
import subprocess
import sys
import tempfile

TARGET = -16.0      # LUFS for speech
SPEECH = -36.0      # momentary LUFS above which a frame counts as speech
PAUSE = -42.0       # room tone in pauses is kept at or below this
WINDOW = 2.5        # seconds each side of the point being leveled
MIN_SPEECH = 0.3    # fraction of the window that must be speech
MAX_BOOST = 14.0    # dB
MAX_CUT = 8.0       # dB
SMOOTH = 1.0        # seconds, gain smoothing
STEP = 0.1          # seconds between gain changes
HIGHPASS = 90       # Hz
LIMIT = 0.708       # linear sample peak, -3 dB
HIGHPASS_AF = f"highpass=f={HIGHPASS}:poles=2,highpass=f={HIGHPASS}:poles=2"


def ffmpeg(*args, **kw):
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True, **kw)


def summary(path):
    """Integrated loudness, loudness range and true peak, from ebur128."""
    err = ffmpeg("-i", path, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-").stderr
    tail = err[err.rfind("Summary:"):]
    get = lambda key: float(re.search(rf"{key}:\s+(-?[\d.]+|-inf)", tail).group(1))
    return get("I"), get("LRA"), get("Peak")


def momentary(path):
    """[(seconds, momentary LUFS)] every 100 ms."""
    out = ffmpeg("-v", "error", "-i", path, "-vn", "-af",
                 "ebur128=metadata=1,ametadata=print:key=lavfi.r128.M:file=-",
                 "-f", "null", "-", check=True).stdout
    frames, t = [], None
    for line in out.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"lavfi\.r128\.M=(\S+)", line)
        if m and t is not None:
            try:
                frames.append((t, float(m.group(1))))
            except ValueError:
                pass
    return frames


def gain_curve(frames):
    """Gain in dB every STEP seconds, smoothed."""
    power = lambda lufs: 10 ** (lufs / 10)
    need = MIN_SPEECH * 2 * WINDOW / STEP
    gains, held, lo, hi = [], 0.0, 0, 0
    for i in range(int(frames[-1][0] / STEP) + 1):
        t = i * STEP
        while lo < len(frames) and frames[lo][0] < t - WINDOW:
            lo += 1
        while hi < len(frames) and frames[hi][0] <= t + WINDOW:
            hi += 1
        speech = [power(m) for _, m in frames[lo:hi] if m > SPEECH]
        if len(speech) >= need:
            level = 10 * math.log10(sum(speech) / len(speech))
            held = max(-MAX_CUT, min(MAX_BOOST, TARGET - level))
            gains.append(held)
        else:
            room = [power(m) for _, m in frames[lo:hi] if -100 < m <= SPEECH]
            level = 10 * math.log10(sum(room) / len(room)) if room else -100
            gains.append(min(held, PAUSE - level))
    k = round(SMOOTH / STEP / 2)
    return [sum(gains[max(0, i - k):i + k + 1]) / len(gains[max(0, i - k):i + k + 1])
            for i in range(len(gains))]


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__.split("\n\n")[1])
    src = sys.argv[1]
    stem, _ = os.path.splitext(src)
    out = sys.argv[2] if len(sys.argv) == 3 else f"{stem}-leveled.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        clean, cmds = os.path.join(tmp, "highpass.wav"), os.path.join(tmp, "gain.cmd")
        ffmpeg("-v", "error", "-y", "-i", src, "-vn", "-af", f"aresample=48000,{HIGHPASS_AF}",
               "-c:a", "pcm_s16le", clean, check=True)
        with open(cmds, "w") as f:
            for i, g in enumerate(gain_curve(momentary(clean))):
                f.write(f"{i * STEP:.2f} volume@ride volume {10 ** (g / 20):.4f};\n")
        ffmpeg("-v", "error", "-y", "-i", src, "-i", clean, "-map", "0:v", "-map", "1:a",
               "-c:v", "copy", "-af",
               f"asendcmd=f={cmds},volume@ride=volume=1:eval=frame,"
               f"alimiter=limit={LIMIT}:attack=5:release=80:level=false",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out, check=True)
    for label, path in (("before", src), ("after", out)):
        i, lra, peak = summary(path)
        print(f"{label:6s} {i:6.1f} LUFS  range {lra:4.1f} LU  true peak {peak:5.1f} dBFS  {path}")


if __name__ == "__main__":
    main()
