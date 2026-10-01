"""The voice gate and the leveling shared by render.py (new takes) and
remix_take.py (takes already recorded).

gate_voice() keeps the mic only where the narrator speaks: where Silero VAD
(whisper.cpp's whisper-vad-speech-segments) hears speech in the mic, minus
where it hears speech in the take's source track, minus whatever else in the
mic sounds like the source (sounds_like). A source with its own voice
plays through the speakers, the mic hears it, and the VAD can't tell that from
narration; mixed, the take had the same speech twice, ~34 ms apart, summing
past full scale (take-20261001-150809, heard as scratchiness). record.sh has
always assumed the narrator doesn't talk over the source's sound; the Narrate
row in the take editor is how to do that afterwards.

level_filter() is the leveling both tracks get: one gain and a limiter.
"""
import array
import json
import math
import os
import re
import subprocess
import tempfile
import wave

VAD_MODEL = os.environ.get("VAD_MODEL", os.path.expanduser("~/.local/share/whisper-models/ggml-silero-v5.1.2.bin"))
# A source track quieter than this has no program, only its room: mixed as
# recorded, not normalized. Leveling one to -16 LUFS turned up its rumble by
# up to ~30 dB (takes measured -46.6 and -48.7 LUFS; a real source, -18.7).
SOURCE_FLOOR = -35.0
VOICE_PRE = "highpass=f=80,acompressor=threshold=-24dB:ratio=3:attack=5:release=120"
# What's left of a mic segment after the source's speech is taken out of it
# must be at least this long to count as narration: the two VAD runs disagree
# by a few hundredths of a second at segment edges, which leaves slivers.
MIN_KEPT_S = 0.3
# The VAD misses some of the source's speech (a phrase at 53.8 s of
# take-20261001-135145), so what's left is also compared with the source
# directly (sounds_like). Measured on five takes, 2026-10-01: the mic's copy of
# the source's voice scored 0.52-0.85 (21 segments), narration 0.00-0.12 (38).
BLEED_R = 0.3
MATCH_RATE = 4000
# Each leveled track ends in this, and so does the take's leveled mix: two
# tracks leveled on their own can sum past full scale. record.sh's mix has
# the same filter.
LIMITER = "alimiter=limit=0.841:attack=5:release=80:level=false"  # -1.5 dB
MAX_LIMITING = 6.0  # dB a track's loudest peak may be pushed into the limiter


def level_filter(path, pre=None, floor=None):
    # The filter that levels a track: `pre`, one gain toward -16 LUFS, and the
    # limiter. loudnorm only measures. It isn't used to apply the gain: asked
    # for linear=true it still falls back to its dynamic mode when the track's
    # loudness range is over its target or the gain would push the peak past
    # the true-peak target, and by those conditions it did on every track of
    # every take up to 2026-10-01. Riding the gain, it raised a source's quiet
    # sibilants 18 dB and its loud words 5 (take-20261001-150809, heard as
    # scratchiness), and raised the room between a narrator's words. The gain
    # is capped so the loudest peak goes at most MAX_LIMITING dB into the
    # limiter: a track with a wide range ends a few dB under -16 instead.
    # A silent track has no loudness to set (measured I is -inf): anull. So
    # does one below floor.
    chain = f"{pre}," if pre else ""
    m1 = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-af",
                         f"{chain}loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                        capture_output=True, text=True).stderr
    ln = json.loads(m1[m1.rindex("{"):m1.rindex("}") + 1])
    if "inf" in ln["input_i"]:
        return "anull"
    loud, peak = float(ln["input_i"]), float(ln["input_tp"])
    if floor is not None and loud < floor:
        print(f"source level: {ln['input_i']} LUFS, below {floor}: not boosted")
        return "anull"
    gain = min(-16.0 - loud, -1.5 - peak + MAX_LIMITING)
    print(f"level: {os.path.basename(path)} at {loud:.1f} LUFS, peak {peak:.1f} dB: {gain:+.1f} dB"
          + ("" if gain == -16.0 - loud else f" (capped; {-16.0 - loud:+.1f} would reach -16)"))
    return f"{chain}volume={gain:.2f}dB,{LIMITER}"


def speech_segments(path):
    # [(start, end)] seconds of speech in an audio file, or None if the VAD
    # can't run (tool or model missing, or it failed). Segments are padded
    # 250 ms so word edges survive, with 300 ms minimum silence so a gate made
    # from them doesn't chatter.
    if not os.path.exists(VAD_MODEL):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        w16 = os.path.join(tmp, "v.wav")
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-ac", "1", "-ar", "16000", w16], check=True)
            r = subprocess.run(["whisper-vad-speech-segments", "-vm", VAD_MODEL, "-f", w16,
                                "--vad-speech-pad-ms", "250", "--vad-min-silence-duration-ms", "300"],
                               capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError):
            return None
    if r.returncode:
        return None
    return [(float(a), float(b)) for a, b in
            re.findall(r"VAD segment \d+: start = ([\d.]+), end = ([\d.]+)", r.stdout + r.stderr)]


def minus(segs, holes):
    # segs with every stretch in holes removed; leftovers under MIN_KEPT_S go.
    out = []
    for a, b in segs:
        pieces = [(a, b)]
        for h0, h1 in holes:
            pieces = [p for x, y in pieces for p in ((x, min(y, h0)), (max(x, h1), y)) if p[1] > p[0]]
        out += [(x, y) for x, y in pieces if y - x >= MIN_KEPT_S]
    return out


def pcm_4k(path):
    # A track as 4 kHz mono samples: enough for sounds_like().
    return array.array("h", subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(MATCH_RATE), "-f", "s16le", "-"],
        capture_output=True).stdout)


def sounds_like(mic, src, a, b):
    # How alike mic and src (pcm_4k) are between a and b seconds: the best
    # |correlation| of the mic's loudest 1.5 s there against the source, slid
    # up to 0.1 s either way (the speakers-to-mic path, and the two tracks'
    # alignment, put the mic's copy a few hundredths of a second off).
    i0, i1 = int(a * MATCH_RATE), min(int(b * MATCH_RATE), len(mic), len(src))
    n = min(i1 - i0, int(1.5 * MATCH_RATE))
    if n < MATCH_RATE // 4:
        return 0.0
    loudest, at = -1, i0
    for s in range(i0, i1 - n + 1, MATCH_RATE // 10):
        e = sum(x * x for x in mic[s:s + n:8])
        if e > loudest:
            loudest, at = e, s
    m = mic[at:at + n]
    nm = math.sqrt(sum(x * x for x in m)) or 1
    best = 0.0
    for lag in range(-MATCH_RATE // 10, MATCH_RATE // 10 + 1):
        if at + lag < 0 or at + lag + n > len(src):
            continue
        s = src[at + lag:at + lag + n]
        ns = math.sqrt(sum(x * x for x in s))
        if ns >= 1:
            best = max(best, abs(sum(x * y for x, y in zip(m, s))) / (nm * ns))
    return best


def gate_voice(voice_wav, src_wav, out_wav):
    # Write voice_wav to out_wav (the same file is fine) silent everywhere but
    # where the narrator speaks, with 50 ms fades. Loudness can't find speech
    # (quiet words can be softer than a noisy room), so the VAD does; ungated,
    # the mic's room would be raised by leveling. src_wav is the take's source
    # track, or a path that doesn't exist. Prints the "voice gate:" log line.
    # Returns False, writing nothing, if the VAD can't run or the voice isn't
    # 16-bit mono; no speech gives a silent track, which leveling leaves alone.
    segs = speech_segments(voice_wav)
    if segs is None:
        print(f"voice gate: no VAD ({VAD_MODEL} or whisper-vad-speech-segments missing); voice not gated")
        return False
    with wave.open(voice_wav) as r:
        vparams = r.getparams()
        v = array.array("h", r.readframes(r.getnframes()))
    if vparams.nchannels != 1 or vparams.sampwidth != 2:
        return False
    heard = sum(b - a for a, b in segs)
    if src_wav and os.path.exists(src_wav):
        segs = minus(segs, speech_segments(src_wav) or [])
        if segs:
            mic, src = pcm_4k(voice_wav), pcm_4k(src_wav)
            segs = [(a, b) for a, b in segs if sounds_like(mic, src, a, b) < BLEED_R]
    fr, gated, speech_s = vparams.framerate, array.array("h", bytes(2 * len(v))), 0.0
    fade = int(0.05 * fr)
    for a, b in segs:
        i0, i1 = max(0, int(a * fr)), min(len(v), int(b * fr))
        if i1 <= i0:
            continue
        gated[i0:i1] = v[i0:i1]
        n = min(fade, (i1 - i0) // 2)
        for k in range(n):
            gated[i0 + k] = int(v[i0 + k] * k / n)
            gated[i1 - 1 - k] = int(v[i1 - 1 - k] * k / n)
        speech_s += (i1 - i0) / fr
    with wave.open(out_wav, "wb") as w:
        w.setparams(vparams)
        w.writeframes(gated.tobytes())
    dropped = max(0.0, heard - sum(b - a for a, b in segs))
    print(f"voice gate: {len(segs)} speech segment{'s' if len(segs) != 1 else ''}, "
          f"{speech_s:.1f}s kept of {len(v) / fr:.1f}s"
          + (f"; {dropped:.1f}s dropped as the source's own voice" if dropped >= 0.05 else ""))
    return True
