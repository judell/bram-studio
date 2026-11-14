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
# The edge pass (edge_pass): how far a change of speaker may move (a
# Setting), how much speech is judged on each side of a candidate edge,
# and what a pause or a sentence end before it adds to its score.
EDGE = float(os.environ.get("VP_EDGE", "2.0"))
EDGE_SIDE = 1.5
PAUSE_BONUS = 0.10  # full at a pause of PAUSE_FULL seconds or more
PAUSE_FULL = 0.3
SENTENCE_BONUS = float(os.environ.get("VP_SENTENCE_BONUS", "0.25"))  # a change of speaker usually lands at a sentence end (tuned on 5 edges, 2026-11-14; 0.25 and 0.3 agree)
# An island is a run with one speaker on both sides of it (A | X | A). It
# stays X only if its whole stretch sounds like X more than A by
# ISLAND_MARGIN; otherwise it's A's. A run longer than ISLAND_MAX seconds is
# a turn, never joined (a long span, cut speech and all, judges badly as
# one). The edge pass never grows an island, and runs first, so a mixed one
# ("Yeah. Nothing. | I went to PKAI.com.") is trimmed before it's judged.
# (2026-11-14: islands like "describe Quadri to us, but" inside Phil's
# question; the edge pass had grown them to sentence ends.)
ISLAND_MAX = float(os.environ.get("VP_ISLAND_MAX", "6.0"))
MASK_CUTS = os.environ.get("VP_MASK_CUTS", "1") != "0"  # silence cut speech before judging voices
ISLAND_MARGIN = float(os.environ.get("VP_ISLAND_MARGIN", "0.1"))
ISLAND_DEBUG = bool(os.environ.get("VP_ISLAND_DEBUG"))
# Word ids whose edge's candidates are printed (stderr) with their scores.
EDGE_DEBUG = {int(x) for x in os.environ.get("VP_EDGE_DEBUG", "").split(",") if x.strip()}
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


def runs_of(seq):
    # [(start, end inclusive)] of consecutive rows with one speaker.
    runs, a = [], 0
    for n in range(1, len(seq) + 1):
        if n == len(seq) or seq[n][3] != seq[a][3]:
            runs.append((a, n - 1))
            a = n
    return runs


def island_pass(seq, prints, embed):
    # Islands (A | X | A) that are short, or don't clearly sound like X as a
    # whole, become A's. Samples are never islands. Repeats until none
    # changes, since joining one can make its neighbours one run.
    joined = 0
    changed = True
    while changed:
        changed = False
        runs = runs_of(seq)
        for k in range(1, len(runs) - 1):
            a, b = runs[k]
            A, X = seq[runs[k - 1][0]][3], seq[a][3]
            if seq[runs[k + 1][0]][3] != A or A == X or any(r[5] for r in seq[a:b + 1]):
                continue
            span = seq[b][2] - seq[a][1]
            margin = None
            if A in prints and X in prints and span <= ISLAND_MAX:
                v = embed(seq[a][1], seq[b][2]) if span >= 0.5 else None
                if v is not None:
                    margin = float(v @ prints[X] - v @ prints[A])
            keep = span > ISLAND_MAX or (margin is not None and margin >= ISLAND_MARGIN)
            if ISLAND_DEBUG and not keep:
                print(f"island {seq[a][0]}-{seq[b][0]} X={X} A={A} span={span:.2f} margin={margin} "
                      f"{'keep' if keep else 'join'} {' '.join(r[6] or '' for r in seq[a:b + 1])[:60]!r}",
                      file=sys.stderr)
            if not keep:
                for r in seq[a:b + 1]:
                    r[3], r[4] = A, 1
                joined += 1
                changed = True
                break
    return joined


def edge_pass(seq, prints, embed):
    # Each change of speaker, decided again. The windows straddle a change,
    # so the last word or two before it tend to go with the next voice
    # (2026-11-14: "agents. I | guess", "built? I should | be doing",
    # "Right. Well, | and many"). Candidates: every gap between words
    # within EDGE seconds of the change, never crossing a sample, never
    # taking a whole turn. Each is scored by the speech just before it
    # (EDGE_SIDE seconds, judged A over B) and just after it (B over A),
    # plus a pause there and a sentence end before it. The change moves to
    # the best. seq rows: [id, t0, t1, speaker, unsure, tagged, text].
    cache = {}

    def emb(a, b):
        key = (round(a, 2), round(b, 2))
        if key not in cache:
            cache[key] = embed(a, b) if b - a > 0 else None
        return cache[key]

    def runs_bounds(n):
        a = n
        while a > 0 and seq[a - 1][3] == seq[n][3]:
            a -= 1
        b = n
        while b + 1 < len(seq) and seq[b + 1][3] == seq[n][3]:
            b += 1
        return a, b

    moved = total = 0
    n = 1
    while n < len(seq):
        A, B = seq[n - 1][3], seq[n][3]
        if A == B or A not in prints or B not in prints:
            n += 1
            continue
        total += 1
        a_start, a_end = runs_bounds(n - 1)
        b_start, b_end = runs_bounds(n)
        # A short island on either side (ISLAND_MAX) may shrink, not grow;
        # a long run between two of another speaker's is a turn.
        left_island = (a_start > 0 and seq[a_start - 1][3] == B
                       and seq[a_end][2] - seq[a_start][1] <= ISLAND_MAX)
        right_island = (b_end + 1 < len(seq) and seq[b_end + 1][3] == A
                        and seq[b_end][2] - seq[b_start][1] <= ISLAND_MAX)
        t_edge = (seq[n - 1][2] + seq[n][1]) / 2
        cl = n
        while not right_island and cl - 1 > a_start and not seq[cl - 1][5] and t_edge - seq[cl - 1][1] <= EDGE:
            cl -= 1
        cr = n
        while not left_island and cr + 1 <= b_end and not seq[cr][5] and seq[cr][2] - t_edge <= EDGE:
            cr += 1
        pa, pb = prints[A], prints[B]
        best_c, best_score = n, None
        for c in range(cl, cr + 1):
            left_end, right_start = seq[c - 1][2], seq[c][1]
            left = emb(max(left_end - EDGE_SIDE, seq[a_start][1]), left_end)
            right = emb(right_start, min(right_start + EDGE_SIDE, seq[b_end][2]))
            sc = 0.0
            if left is not None:
                sc += float(left @ pa - left @ pb)
            if right is not None:
                sc += float(right @ pb - right @ pa)
            gap = max(0.0, right_start - left_end)
            sc += PAUSE_BONUS * min(gap / PAUSE_FULL, 1.0)
            if (seq[c - 1][6] or "").rstrip().endswith((".", "?", "!")):
                sc += SENTENCE_BONUS
            if EDGE_DEBUG & {seq[k][0] for k in range(cl - 1, cr + 1)}:
                print(f"edge {seq[n - 1][0]}|{seq[n][0]} cand before {seq[c][0]} {seq[c][6]!r}: "
                      f"left={float(left @ pa - left @ pb) if left is not None else None} "
                      f"right={float(right @ pb - right @ pa) if right is not None else None} "
                      f"gap={gap:.2f} total={sc:.3f}", file=sys.stderr)
            if best_score is None or sc > best_score:
                best_c, best_score = c, sc
        if best_c < n:
            for s in seq[best_c:n]:
                s[3] = B
            moved += 1
        elif best_c > n:
            for s in seq[n:best_c]:
                s[3] = A
            moved += 1
        n = max(best_c, n) + 1
    return {"moved": moved, "total": total}


def main():
    wav, db_path, source = sys.argv[1:4]
    w = wave.open(wav)
    sr = w.getframerate()
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    dur = len(audio) / sr
    db = sqlite3.connect(db_path)
    words = db.execute("SELECT id, i, t0, t1, kept, speaker_id, speaker_how, coalesce(shown, text) FROM words "
                       "WHERE source = ? ORDER BY i", (source,)).fetchall()
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=4))

    # Cut speech is silenced before any voice is judged: the transcript is
    # of the edit, and what was cut is often someone else talking over the
    # speaker kept (2026-11-14: "As you have agents that", "and logging and
    # tracking", "you start" each went to the voice in the cut beside them).
    # A run of cut words is silenced from its first word's start to its last
    # word's end, and never into a kept word.
    if MASK_CUTS:
        audio = audio.copy()
        rng = np.random.default_rng(0)
        k = 0
        while k < len(words):
            if words[k][4]:
                k += 1
                continue
            j = k
            while j + 1 < len(words) and not words[j + 1][4]:
                j += 1
            a = words[k][2]
            b = words[j][3]
            if k > 0:
                a = max(a, words[k - 1][3])
            if j + 1 < len(words):
                b = min(b, words[j + 1][2])
            if b > a:
                # Faint noise, not zeros: the speaker model fails on exact
                # digital silence. Far below QUIET, so a window that is all
                # cut is skipped like any silent one.
                n = int(b * sr) - int(a * sr)
                audio[int(a * sr):int(b * sr)] = rng.normal(0, 1e-4, n).astype(np.float32)
            k = j + 1

    failed = []

    def embed(a, b):
        seg = audio[int(a * sr):int(b * sr)]
        if len(seg) < sr * 0.3 or np.sqrt(np.mean(seg ** 2)) < QUIET:
            return None
        s = ext.create_stream()
        s.accept_waveform(sr, seg)
        s.input_finished()
        try:
            v = np.array(ext.compute(s))
        except RuntimeError as e:
            # The model refuses some segments (seen once cut speech is
            # silenced): no judgment there, as for a silent window.
            failed.append((round(a, 2), round(b, 2)))
            if len(failed) <= 3:
                print(f"embed failed {a:.2f}-{b:.2f}: {str(e)[:80]}", file=sys.stderr)
            return None
        return v / (np.linalg.norm(v) or 1)

    # Examples: runs of consecutive tagged words with one speaker.
    stretches = collections.defaultdict(list)
    run = None
    for wid, i, t0, t1, kept, spk, how, _ in words:
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
    seq = []  # [word_id, t0, t1, speaker, unsure, tagged, text]
    for wid, i, t0, t1, kept, spk, how, text in words:
        if not kept:
            continue
        if how == "tag" and spk is not None:
            seq.append([wid, t0, t1, spk, 0, True, text])
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
        seq.append([wid, t0, t1, who, unsure, False, text])

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

    edges = edge_pass(seq, prints, embed) if EDGE > 0 else {"moved": 0, "total": 0}
    edges["islandsJoined"] = island_pass(seq, prints, embed)

    edges["embedFailed"] = len(failed)
    print(json.dumps({"speakers": speakers, "windows": len(best), "edges": edges,
                      "assign": [[s[0], s[3], s[4]] for s in seq if not s[5] and s[3] is not None]}))


if __name__ == "__main__":
    main()
