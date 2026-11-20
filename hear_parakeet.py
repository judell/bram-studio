#!/usr/bin/env python3
"""Hear a recording with NVIDIA Parakeet: a second opinion on the words.

    <venv python> -I hear_parakeet.py <wav 16 kHz mono> <out.json>

Writes the neutral opinion form serve_media.py's write_opinion writes:
{"engine": "parakeet", "at", "secs", "words": [[text, t0, t1], ...]}, with
times in seconds of the wav. Prints "done_s=<seconds heard>" to stderr after
each stretch, which the server turns into progress.

Why Parakeet: it is a transducer, not whisper-derived, so its mistakes are its
own and rarely the same as whisper's; on a 3 minute clip it takes about 7 s on
the CPU. The model is parakeet-tdt-0.6b-v3 (int8, for sherpa-onnx), unpacked
in ~/.local/share/speech-models/ (docs/get-me-going.md says how to fetch it
from https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models).

Why stretches: the whole hour in one decode would hold it all in memory at
once. The audio is cut at quiet points (ffmpeg silencedetect) into stretches
of at most about 60 s, each decoded on its own stream and its times offset by
where it starts.

Runs under the speech-tools venv (sherpa-onnx, numpy), which serve_media.py
finds as SPEECH_PYTHON.
"""
import json
import os
import re
import subprocess
import sys
import time
import wave

import numpy as np
import sherpa_onnx

MODEL = os.environ.get("PARAKEET_MODEL", os.path.expanduser(
    "~/.local/share/speech-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"))
STRETCH = 60.0  # seconds, at most, per decode


def quiet_midpoints(wav):
    out = subprocess.run(["ffmpeg", "-nostdin", "-i", wav, "-af", "silencedetect=noise=-35dB:d=0.3",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    mids, start = [], None
    for m in re.finditer(r"silence_(start|end): ([\d.]+)", out):
        if m.group(1) == "start":
            start = float(m.group(2))
        elif start is not None:
            mids.append((start + float(m.group(2))) / 2)
            start = None
    return mids


def cuts(total, mids):
    # Stretch boundaries: the last quiet midpoint before each STRETCH mark
    # (after the previous cut), else the mark itself.
    bounds, at = [0.0], 0.0
    while total - at > STRETCH:
        before = [m for m in mids if at + 5 < m <= at + STRETCH]
        at = before[-1] if before else at + STRETCH
        bounds.append(at)
    bounds.append(total)
    return bounds


def words_of(res, offset):
    words = []
    for tok, t, d in zip(res.tokens, res.timestamps, res.durations):
        t0, t1 = offset + t, offset + t + d
        if tok[:1] in (" ", "▁") or not words:
            words.append([tok.lstrip(" ▁"), t0, t1])
        else:
            words[-1][0] += tok
            words[-1][2] = t1
    return [[w, round(a, 3), round(b, 3)] for w, a, b in words if w]


def main():
    wav, out = sys.argv[1], sys.argv[2]
    t_start = time.time()
    with wave.open(wav) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    total = len(x) / sr
    rec = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=f"{MODEL}/encoder.int8.onnx", decoder=f"{MODEL}/decoder.int8.onnx",
        joiner=f"{MODEL}/joiner.int8.onnx", tokens=f"{MODEL}/tokens.txt",
        num_threads=8, model_type="nemo_transducer")
    bounds = cuts(total, quiet_midpoints(wav))
    words = []
    for a, b in zip(bounds, bounds[1:]):
        s = rec.create_stream()
        s.accept_waveform(sr, x[int(a * sr):int(b * sr)])
        rec.decode_stream(s)
        words += words_of(s.result, a)
        print(f"done_s={b:.1f}", file=sys.stderr, flush=True)
    tmp = os.path.join(os.path.dirname(os.path.abspath(out)), ".opinion-parakeet.tmp")
    with open(tmp, "w") as f:
        json.dump({"engine": "parakeet", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "secs": round(time.time() - t_start, 1), "words": words}, f)
    os.replace(tmp, out)


main()
