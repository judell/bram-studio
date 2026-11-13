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
TOKEN = 0.15  # how long a word lasts after its last token's timestamp
LEAD = 0.06  # how long before its first token's timestamp a word starts
FRAME = 0.01  # the quietness search's step
KEY = re.compile(r"[^a-z']+")


def key(text):
    return KEY.sub("", text.lower())


def heard_words(rec, audio, sr, a, b):
    # The model's words in audio[a:b], each [key, start, last token time].
    s = rec.create_stream()
    s.accept_waveform(sr, audio[int(a * sr):int(b * sr)])
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
    seg = audio[int(lo * sr):int(hi * sr)]
    frames = len(seg) // n
    if frames < 1:
        return (lo + hi) / 2
    rms = np.sqrt(np.mean(seg[:frames * n].reshape(frames, n) ** 2, axis=1))
    return lo + (int(np.argmin(rms)) + 0.5) * FRAME


def voiced(audio, sr, lo, hi):
    # The first and last FRAME in [lo, hi] louder than -35 dB (silencedetect's
    # threshold), or None when it is all quiet.
    n = int(FRAME * sr)
    seg = audio[int(lo * sr):int(hi * sr)]
    frames = len(seg) // n
    if frames < 1:
        return None
    rms = np.sqrt(np.mean(seg[:frames * n].reshape(frames, n) ** 2, axis=1))
    loud = np.nonzero(rms > 10 ** (-35 / 20))[0]
    if not len(loud):
        return None
    return lo + loud[0] * FRAME, lo + (loud[-1] + 1) * FRAME


def word_spans(ours, at, audio, sr):
    # [start, end] for each of our words, by the sound. A word the model heard
    # (at: our i -> its word) has its own times. Words it missed ("uh", which
    # it often doesn't hear at all) lie between the heard words around them:
    # in the voiced part of that gap, shared out by whisper's durations. Words
    # outside the first and last heard word get no span.
    spans = {}
    anchors = [n for n, r in enumerate(ours) if r[0] in at]
    for n in anchors:
        h = at[ours[n][0]]
        spans[ours[n][0]] = (h[1], h[2] + TOKEN)
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
        span = word_spans(ours, at, audio, sr)
        sp, sn = span.get(prev[0]), span.get(nxt[0])
        if not sp or not sn:
            print(json.dumps({"i": i, "t": round(whisper_t, 3), "method": "unaligned",
                              "before": prev[3], "after": nxt[3]}), flush=True)
            continue
        method = "ctc" if prev[0] in at and nxt[0] in at else "ctc-gap"
        lo, hi = sp[1], sn[0] - LEAD
        if hi < lo:  # words run together: cut where one ends and the next begins
            lo = hi = (lo + hi) / 2
        t = quietest(audio, sr, max(0.0, lo), min(dur, hi))
        print(json.dumps({"i": i, "t": round(t, 3), "method": method, "before": prev[3], "after": nxt[3]}),
              flush=True)


if __name__ == "__main__":
    main()
