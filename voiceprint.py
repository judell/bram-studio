#!/usr/bin/env python3
"""Who's speaking, from voiceprints: speakers you tagged, carried to the rest.

    <venv python> voiceprint.py <wav> <db> <source>

Reads the source's words (serve_media.py's words table). Stretches you
tagged (speaker_how = 'tag') are each speaker's examples: their audio, in
windows, averaged into a voiceprint (a speaker embedding). Then every window
of the recording is compared with each voiceprint, and every kept word you
didn't tag goes to the speaker its windows match best. Prints one JSON object:

    {"speakers": {id: {"taggedSecs", "used"}}, "windows": N,
     "assign": [[word_id, speaker_id, unsure], ...]}

and progress lines "progress <done> <total>" on stderr.

The model is NeMo TitaNet-small under sherpa-onnx (CPU). On the AIW
recording, with 12 s of examples each, it put 45 of 46 held-out 1.5 s
windows with the right one of two speakers, at 7 ms a window (2026-11-13).

Durations are the Settings page's (Podcast editing), passed in by
serve_media.py: VP_WINDOW and VP_STEP (seconds of audio per voiceprint and
between them), VP_MIN_TURN (a run of words shorter than this joins its
neighbours), VP_MIN_TAGGED (a speaker needs this much tagged speech to be
used). Runs under the speech-tools venv (~/.local/share/bram-studio/venv).
"""
import collections
import json
import os
import sqlite3
import sys
import wave

import numpy as np
import sherpa_onnx

MODEL = os.environ.get("SPEAKER_MODEL", os.path.expanduser(
    "~/.local/share/speech-models/nemo_en_titanet_small.onnx"))
WINDOW = float(os.environ.get("VP_WINDOW", "1.5"))
STEP = float(os.environ.get("VP_STEP", "0.5"))
MIN_TURN = float(os.environ.get("VP_MIN_TURN", "1.0"))
MIN_TAGGED = float(os.environ.get("VP_MIN_TAGGED", "10"))
QUIET = 10 ** (-40 / 20)  # a window quieter than this (RMS) has no voice to match
# A word is "unsure" when its windows barely prefer its speaker over the next
# best (mean cosine margin), or match no one well. Not a duration; tuned
# from the probe above (same speaker ~0.43, different ~0.19).
UNSURE_MARGIN = 0.05
UNSURE_SCORE = 0.25
TRIM = 0.2  # seconds shaved off each end of a tagged stretch: whisper's word times can be off


CHECK = "--check" in sys.argv


def check_samples(stretches, prints, ids, mat, embed):
    # --check: is each sample stretch the voice it's tagged as? Every window
    # of it is scored against every voiceprint (built from all the samples,
    # these included). A run of two or more windows in a row that another
    # speaker wins is flagged: a lone window that loses is usually two close
    # scores, noise (2026-11-14: a run is what Liam's sample with Elliot and
    # Doc in it looked like). Prints {"stretches": [{speaker, firstWord, t0,
    # t1, flags: [{t0, t1, like, windows}]}]}.
    out = []
    for spk, runs in stretches.items():
        for _, a, b, _, wid in runs:
            wins = []
            t = a + TRIM
            while t + WINDOW <= b - TRIM:
                v = embed(t, t + WINDOW)
                if v is not None:
                    sims = mat @ v
                    best = ids[int(np.argmax(sims))]
                    own = float(sims[ids.index(spk)]) if spk in prints else 0.0
                    wins.append((t, best, float(sims.max()) - own))
                t += STEP
            flags, k = [], 0
            while k < len(wins):
                if wins[k][1] == spk or spk not in prints:
                    k += 1
                    continue
                j = k
                while j + 1 < len(wins) and wins[j + 1][1] != spk:
                    j += 1
                if j > k:
                    like = collections.Counter(w[1] for w in wins[k:j + 1]).most_common(1)[0][0]
                    t0, t1 = round(wins[k][0], 2), round(wins[j][0] + WINDOW, 2)
                    # Overlapping the last flag, same voice (one window in
                    # between went the sample's way): one flag, not two.
                    if flags and flags[-1]["like"] == like and t0 <= flags[-1]["t1"]:
                        flags[-1]["t1"] = t1
                        flags[-1]["windows"] += j - k + 1
                    else:
                        flags.append({"t0": t0, "t1": t1, "like": like, "windows": j - k + 1})
                k = j + 1
            out.append({"speaker": spk, "firstWord": wid, "t0": round(a, 2), "t1": round(b, 2), "flags": flags})
    return {"stretches": out}


def main():
    wav, db_path, source = sys.argv[1:4]
    w = wave.open(wav)
    sr = w.getframerate()
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    dur = len(audio) / sr
    db = sqlite3.connect(db_path)
    words = db.execute("SELECT id, i, t0, t1, kept, speaker_id, speaker_how FROM words "
                       "WHERE source = ? ORDER BY i", (source,)).fetchall()
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=4))

    def embed(a, b):
        seg = audio[int(a * sr):int(b * sr)]
        if len(seg) < sr * 0.3 or np.sqrt(np.mean(seg ** 2)) < QUIET:
            return None
        s = ext.create_stream()
        s.accept_waveform(sr, seg)
        s.input_finished()
        v = np.array(ext.compute(s))
        return v / (np.linalg.norm(v) or 1)

    # Examples: runs of consecutive tagged words with one speaker.
    stretches = collections.defaultdict(list)
    run = None
    for wid, i, t0, t1, kept, spk, how in words:
        if how == "tag" and spk is not None:
            if run and run[0] == spk and run[3] == i - 1:
                run[2], run[3] = t1, i
            else:
                run = [spk, t0, t1, i, wid]  # wid: the stretch's first word
                stretches[spk].append(run)
        else:
            run = None
    speakers, prints = {}, {}
    for spk, runs in stretches.items():
        secs = sum(r[2] - r[1] for r in runs)
        vecs = []
        for _, a, b, _, _ in runs:
            a, b = a + TRIM, b - TRIM
            t = a
            while t + WINDOW <= b:
                v = embed(t, t + WINDOW)
                if v is not None:
                    vecs.append(v)
                t += STEP
            if b - a >= 0.5 and not vecs:
                v = embed(a, b)
                if v is not None:
                    vecs.append(v)
        used = secs >= MIN_TAGGED and bool(vecs)
        speakers[spk] = {"taggedSecs": round(secs, 1), "used": used}
        if used:
            c = np.mean(vecs, axis=0)
            prints[spk] = c / np.linalg.norm(c)
    if not prints:
        print(json.dumps({"speakers": speakers, "windows": 0, "assign": [],
                          "error": f"no speaker has {MIN_TAGGED:g} s of tagged speech yet"}))
        return
    ids = list(prints)
    mat = np.stack([prints[k] for k in ids])
    if CHECK:
        print(json.dumps(check_samples(stretches, prints, ids, mat, embed)))
        return

    # Every window of the recording against every voiceprint.
    starts = np.arange(0, max(dur - WINDOW, 0) + 1e-9, STEP)
    best, margin, score = {}, {}, {}
    for n, t in enumerate(starts):
        v = embed(t, t + WINDOW)
        if v is not None:
            sims = mat @ v
            order = np.argsort(sims)[::-1]
            best[n] = ids[order[0]]
            score[n] = float(sims[order[0]])
            margin[n] = float(sims[order[0]] - (sims[order[1]] if len(order) > 1 else 0))
        if n % 200 == 0:
            print(f"progress {n} {len(starts)}", file=sys.stderr, flush=True)

    # Each kept word: the windows around it vote, weighted by margin.
    seq = []  # [word_id, t0, t1, speaker, unsure, tagged]
    for wid, i, t0, t1, kept, spk, how in words:
        if not kept:
            continue
        if how == "tag" and spk is not None:
            seq.append([wid, t0, t1, spk, 0, True])
            continue
        lo = max(0, int((t0 - 0.25 - WINDOW / 2) / STEP))
        hi = int((t1 + 0.25 - WINDOW / 2) / STEP) + 1
        votes = collections.Counter()
        ms, ss = [], []
        for n in range(lo, min(hi + 1, len(starts))):
            if n in best:
                votes[best[n]] += margin[n] + 1e-3
                ms.append(margin[n])
                ss.append(score[n])
        if votes:
            who = votes.most_common(1)[0][0]
            unsure = int(np.mean(ms) < UNSURE_MARGIN or np.mean(ss) < UNSURE_SCORE)
        else:
            who, unsure = None, 1
        seq.append([wid, t0, t1, who, unsure, False])

    # Words no window covered take the speaker before them (or after).
    for n, s in enumerate(seq):
        if s[3] is None:
            near = next((seq[k][3] for k in range(n - 1, -1, -1) if seq[k][3] is not None), None) or \
                next((seq[k][3] for k in range(n + 1, len(seq)) if seq[k][3] is not None), None)
            s[3] = near
    # Runs shorter than MIN_TURN (untagged) join a neighbour.
    changed = True
    while changed:
        changed = False
        runs, start = [], 0
        for n in range(1, len(seq) + 1):
            if n == len(seq) or seq[n][3] != seq[start][3]:
                runs.append((start, n))
                start = n
        for k, (a, b) in enumerate(runs):
            if seq[b - 1][2] - seq[a][1] >= MIN_TURN or any(s[5] for s in seq[a:b]):
                continue
            prev = seq[runs[k - 1][0]][3] if k > 0 else None
            nxt = seq[runs[k + 1][0]][3] if k + 1 < len(runs) else None
            to = prev if prev is not None and (prev == nxt or nxt is None) else (nxt if nxt is not None else prev)
            if to is not None and to != seq[a][3]:
                for s in seq[a:b]:
                    s[3], s[4] = to, 1
                changed = True
                break

    print(json.dumps({"speakers": speakers, "windows": len(best),
                      "assign": [[s[0], s[3], s[4]] for s in seq if not s[5] and s[3] is not None]}))


if __name__ == "__main__":
    main()
