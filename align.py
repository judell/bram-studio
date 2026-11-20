#!/usr/bin/env python3
"""Place cut edges by the sound: where, between two words, a cut should fall.

    <venv python> align.py <wav> <db> <source> <i> [<i> ...]

Each <i> names a boundary: the gap between word i-1 and word i of <source> in
the project database's words table (serve_media.py). For each, it prints one
JSON line {i, t, method, before, after}: t is the source second to cut at,
the quietest 10 ms of audio between the end of word i-1 and the start of word
i, as a speech model hears them.

Why: whisper's word times run early by up to a second in places, and cuts
placed by them removed the wrong words ("contacts me and says, uh" became
"contacts us, uh" in a rendered test, 2026-11-13). A CTC model (sherpa-onnx,
NeMo conformer-small) gives a time per token from the audio frames; on the
AIW recording its word starts fall where silencedetect's quiet ends, which
whisper's didn't. Its spelling is rough ("hillmore", "piecast"), so its
words are matched to the transcript's (difflib on letters) and only its
times are used.

method is "ctc" when the model heard both words, "ctc-gap" when it missed one
(often a filler: it doesn't hear "uh") and the word was placed in the voiced
part of the gap between the heard words around it (word_spans), and
"unaligned" when a word lies outside every heard word: t is then whisper's
boundary, and the page flags the cut.

Runs under the speech-tools venv (~/.local/share/bram-studio/venv: sherpa-onnx,
numpy), which serve_media.py finds as SPEECH_PYTHON.
"""
import difflib
import json
import os
import re
import sqlite3
import sys
import wave

import numpy as np
import sherpa_onnx

MODEL = os.environ.get("CTC_MODEL", os.path.expanduser(
    "~/.local/share/speech-models/sherpa-onnx-nemo-ctc-en-conformer-small"))
WINDOW = 4.0  # seconds of audio on each side of whisper's boundary (its times can be ~1 s off)
# CTC timestamps mark one frame inside each token, not where a word starts or
# ends: a word runs on past its last token's frame, and starts a little
# before its first. With 0.08 s, the cut after "today." left its tail ("hey")
# in a round-trip check (2026-11-13).
# So a heard word's ends are found in the sound: it ends where the audio goes
# quiet after its last token, and starts where the quiet before its first
# ends (quiet_after, quiet_before). A single-token word like "between" runs
# ~0.3 s past its timestamp, and a fixed 0.15 s cut into it (2026-11-13). The
# fixed allowances below are used only when a word runs straight into the
# next, with no quiet between.
TOKEN = 0.15  # how long a word lasts after its last token's timestamp, without quiet to go by
LEAD = 0.06  # how long before its first token's timestamp a word starts, likewise
QUIET = 10 ** (-35 / 20)  # silencedetect's threshold, as an RMS level
# A word-final fricative is quiet broadband but not silent above 3 kHz: the S
# of "AWS." (123.90-124.10 s of ioh.mp4) measured -44 to -50 dB there in 50 ms
# windows, while true pauses between ioh.mp4's words measured -55 dB or lower
# (median -74; 45 broadband-quiet edge windows, only 2 above -50). A frame is
# quiet only when it is under both thresholds.
QUIET_HIGH = 10 ** (-50 / 20)
HIGH_HZ = 3000  # the high band is what a one-pole high-pass at this frequency passes
SNAP = 0.15  # how far a placed-in-the-gap edge may move to reach a pause (seconds)
# Quiet must last this long to count as a pause: shorter dips happen inside
# words (the stop in "be-tween" put a word's end at its middle, 2026-11-13).
# The Settings page's "pause between words" (pauseMs, default 80 ms, which is
# silencedetect's d=0.08 that found the real pauses), passed by serve_media.py.
FRAME = 0.01  # the quietness search's step
PAUSE = max(1, round(int(os.environ.get("ALIGN_PAUSE_MS", "80")) / 1000 / FRAME))  # in FRAMEs
KEY = re.compile(r"[^a-z']+")


def key(text):
    return KEY.sub("", text.lower())


def heard_words(rec, audio, sr, a, b):
    # The model's words in audio[a:b], each [key, start, last token time].
    s = rec.create_stream()
    s.accept_waveform(sr, audio[0, int(a * sr):int(b * sr)])
    rec.decode_stream(s)
    words = []
    for tok, ts in zip(s.result.tokens, s.result.timestamps):
        if tok.startswith(" ") or tok.startswith("▁") or not words:
            words.append([tok, a + ts, a + ts])
        else:
            words[-1][0] += tok
            words[-1][2] = a + ts
    return [[key(w), s0, s1] for w, s0, s1 in words if key(w)]


def quietest(audio, sr, lo, hi):
    # The centre of the quietest FRAME-long stretch in [lo, hi].
    if hi - lo < 2 * FRAME:
        return (lo + hi) / 2
    n = int(FRAME * sr)
    seg = audio[:, int(lo * sr):int(hi * sr)]
    frames = seg.shape[1] // n
    if frames < 1:
        return (lo + hi) / 2
    rms = np.sum(frame_levels(seg, frames, n), axis=0)
    return lo + (int(np.argmin(rms)) + 0.5) * FRAME


def frame_levels(seg, frames, n):
    # [broadband RMS, high-band RMS] per FRAME of seg (2 rows: raw, high-passed).
    return np.sqrt(np.mean(seg[:, :frames * n].reshape(2, frames, n) ** 2, axis=2))


def split_bands(audio, sr):
    # audio as 2 rows: the samples, and the samples through a one-pole
    # high-pass at HIGH_HZ (y[n] = a * (y[n-1] + x[n] - x[n-1])), so the
    # frame levels can see fricative energy that broadband RMS buries.
    # Applied as its frequency response (an FFT over the whole file: a loop
    # over ~3M samples took 4 s per run); the response dies in a few ms, so
    # the wrap-around at the end is nothing.
    a = np.exp(-2 * np.pi * HIGH_HZ / sr)
    n = 1 << (len(audio) - 1).bit_length()  # a power of two: any other length can take a minute
    z = np.exp(-2j * np.pi * np.fft.rfftfreq(n))
    y = np.fft.irfft(np.fft.rfft(audio.astype(np.float64), n) * a * (1 - z) / (1 - a * z), n)[:len(audio)]
    return np.stack([audio, y.astype(np.float32)])


def voiced(audio, sr, lo, hi):
    # The first and last FRAME in [lo, hi] that is not quiet (over -35 dB
    # broadband or over QUIET_HIGH above 3 kHz), or None when it is all quiet.
    rms = frame_rms(audio, sr, lo, hi)
    loud = np.nonzero(~rms)[0]
    if not len(loud):
        return None
    return lo + loud[0] * FRAME, lo + (loud[-1] + 1) * FRAME


def frame_rms(audio, sr, lo, hi):
    # Per FRAME in [lo, hi]: True when quiet in both bands.
    n = int(FRAME * sr)
    seg = audio[:, int(lo * sr):int(hi * sr)]
    frames = seg.shape[1] // n
    if not frames:
        return np.array([], dtype=bool)
    lv = frame_levels(seg, frames, n)
    return (lv[0] < QUIET) & (lv[1] < QUIET_HIGH)


def pauses(quiet_frames):
    # [(first frame, end frame)] of runs of at least PAUSE quiet frames.
    runs, start = [], None
    for k, quiet in enumerate(list(quiet_frames) + [False]):
        if quiet and start is None:
            start = k
        elif not quiet and start is not None:
            if k - start >= PAUSE:
                runs.append((start, k))
            start = None
    return runs


def to_pause(audio, sr, t, dur):
    # A "ctc-gap" edge is a word's share of the voiced gap, so it can fall
    # inside sound ("AWS." / "I've": 123.937, with the S running to 124.04 and
    # the pause after it). When t is not in a quiet frame, move it to the
    # quietest frame of the nearest pause within SNAP seconds; else keep it.
    lo, hi = max(0.0, t - SNAP - PAUSE * FRAME), min(dur, t + SNAP + PAUSE * FRAME)
    q = frame_rms(audio, sr, lo, hi)
    k = int((t - lo) / FRAME)
    if len(q) and 0 <= k < len(q) and q[k]:
        return t
    best = None
    for a, b in pauses(q):
        d = 0.0 if a <= k < b else min(abs(a - k), abs(b - 1 - k))
        if d * FRAME <= SNAP and (best is None or d < best[0]):
            best = (d, a, b)
    if best is None:
        return t
    return quietest(audio, sr, lo + best[1] * FRAME, lo + best[2] * FRAME)


def quiet_after(audio, sr, t, cap):
    # Where the first pause after t (a word's last token) begins, before cap
    # (the next heard word's first token); else the TOKEN allowance.
    p = pauses(frame_rms(audio, sr, t + 0.03, cap))
    return t + 0.03 + p[0][0] * FRAME if p else min(t + TOKEN, cap)


def quiet_before(audio, sr, t, floor):
    # Where the last pause before t (a word's first token) ends, after floor
    # (the previous heard word's last token); else the LEAD allowance.
    p = pauses(frame_rms(audio, sr, floor, t))
    return floor + p[-1][1] * FRAME if p else max(t - LEAD, floor)


def word_spans(ours, at, heard, audio, sr):
    # [start, end] for each of our words, by the sound. A word the model heard
    # (at: our i -> its word) has its own times. Words it missed ("uh", which
    # it often doesn't hear at all) lie between the heard words around them:
    # in the voiced part of that gap, shared out by whisper's durations. Words
    # outside the first and last heard word get no span.
    spans = {}
    anchors = [n for n, r in enumerate(ours) if r[0] in at]
    place = {id(h): k for k, h in enumerate(heard)}
    for n in anchors:
        h = at[ours[n][0]]
        k = place[id(h)]
        floor = heard[k - 1][2] + 0.03 if k > 0 else h[1] - 0.4
        cap = heard[k + 1][1] if k + 1 < len(heard) else h[2] + 0.6
        spans[ours[n][0]] = (quiet_before(audio, sr, h[1], max(floor, h[1] - 0.4)),
                             quiet_after(audio, sr, h[2], max(cap, h[2] + 0.04)))
    for a, b in zip(anchors, anchors[1:]):
        missed = ours[a + 1:b]
        if not missed:
            continue
        g0, g1 = spans[ours[a][0]][1], spans[ours[b][0]][0]
        v = voiced(audio, sr, g0, g1) if g1 > g0 else None
        v0, v1 = v if v else (g0, max(g0, g1))
        total = sum(max(r[2] - r[1], 0.05) for r in missed)
        t = v0
        for r in missed:
            d = (v1 - v0) * max(r[2] - r[1], 0.05) / total
            spans[r[0]] = (t, t + d)
            t += d
    return spans


def main():
    wav, db_path, source, *boundaries = sys.argv[1:]
    w = wave.open(wav)
    sr = w.getframerate()
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    dur = len(audio) / sr
    audio = split_bands(audio, sr)
    db = sqlite3.connect(db_path)
    words = db.execute("SELECT i, t0, t1, text FROM words WHERE source = ? ORDER BY i", (source,)).fetchall()
    by_i = {r[0]: n for n, r in enumerate(words)}
    rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
        model=os.path.join(MODEL, "model.int8.onnx"), tokens=os.path.join(MODEL, "tokens.txt"), num_threads=4)
    for i in map(int, boundaries):
        n = by_i.get(i)
        if n is None or n == 0:
            print(json.dumps({"i": i, "t": 0.0, "method": "start"}), flush=True)
            continue
        prev, nxt = words[n - 1], words[n]
        whisper_t = (prev[2] + nxt[1]) / 2
        a, b = max(0.0, whisper_t - WINDOW), min(dur, whisper_t + WINDOW)
        # The transcript's words that could be in the window (with slack,
        # since whisper's times are what's in doubt).
        ours = [r for r in words if a - 1.5 <= r[1] <= b + 1.5]
        heard = heard_words(rec, audio, sr, a, b)
        m = difflib.SequenceMatcher(None, [key(r[3]) for r in ours], [h[0] for h in heard], autojunk=False)
        at = {}
        for blk in m.get_matching_blocks():
            for k in range(blk.size):
                at[ours[blk.a + k][0]] = heard[blk.b + k]
        span = word_spans(ours, at, heard, audio, sr)
        sp, sn = span.get(prev[0]), span.get(nxt[0])
        if not sp or not sn:
            print(json.dumps({"i": i, "t": round(whisper_t, 3), "method": "unaligned",
                              "before": prev[3], "after": nxt[3]}), flush=True)
            continue
        method = "ctc" if prev[0] in at and nxt[0] in at else "ctc-gap"
        lo, hi = sp[1], sn[0]
        if hi < lo:  # words run together: cut where one ends and the next begins
            lo = hi = (lo + hi) / 2
        t = quietest(audio, sr, max(0.0, lo), min(dur, hi))
        if method == "ctc-gap":
            t = to_pause(audio, sr, t, dur)
        print(json.dumps({"i": i, "t": round(t, 3), "method": method, "before": prev[3], "after": nxt[3]}),
              flush=True)


if __name__ == "__main__":
    main()
