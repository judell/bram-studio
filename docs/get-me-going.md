# Get me going

Playbook for any coding agent with shell access in this repo, when the user
says "Get me going." The README is the reference; this is the procedure.

## 1. Start the server

Run `./start.sh`. It starts `serve_media.py` in the background, or reports that
it is already running. If it says the server is running code older than the
files on disk, suggest `./start.sh --restart`. Never restart while a narration,
render or export is under way (`start.sh` refuses itself in that case).

Confirm port 8765 answers (for example `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/`).

The editor tab in the user's browser recovers on its own once the server is
up. Say so; don't walk the user through reloading.

## 2. Inventory by tier

Check each tier and report what works **now** and what each missing piece
would **add**. Don't present a wall of prerequisites.

- **Core screencast** (cut scenes, pauses, callouts, final video): `ffmpeg`;
  Python 3 with Pillow.
- **Narration:** Apple Command Line Tools (`swiftc`; `serve_media.py` compiles
  `record_native.swift` on first use) and a microphone.
- **Naming takes and transcription:** `whisper-cli` and a model in
  `~/.local/share/whisper-models/` (`ggml-small.en.bin` at minimum;
  `ggml-large-v3-turbo` preferred for podcasts).
- **Podcast cut-edge alignment** (optional; without it, edges fall back to
  whisper's times and are flagged): the venv at `~/.local/share/bram-studio/venv`
  with sherpa-onnx and numpy, and the CTC model under
  `~/.local/share/speech-models/`.
- **Final transcript speaker matching** (optional; the route refuses politely
  without it): `nemo_en_titanet_small.onnx` in `~/.local/share/speech-models/`.
- **`remix_take.py --regate` only:** the Silero VAD model at
  `~/.local/share/whisper-models/ggml-silero-v5.1.2.bin` (about 0.9 MB, from
  ggml-org/whisper-vad on Hugging Face).

Exact install commands for the podcast pieces are in the README, in "Podcast
in detail" under **Models**:

```sh
python3 -m venv ~/.local/share/bram-studio/venv
~/.local/share/bram-studio/venv/bin/pip install sherpa-onnx numpy
mkdir -p ~/.local/share/speech-models && cd ~/.local/share/speech-models
curl -L https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-ctc-en-conformer-small.tar.bz2 | tar xj
curl -L -o nemo_en_titanet_small.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
```

The Command Line Tools install is `xcode-select --install` (about 2.4 GB, not
the full Xcode app).

## 3. Offer installs, confirm first

For anything missing that the user would want: show the commands and the
download sizes (where the README doesn't give one, check with
`curl -sIL <url> | grep -i content-length` before quoting), ask once, then
run and report what happened. Never install
unasked. If `brew` itself is missing, say so and stop; installing Homebrew is
the user's call.

## 4. Match the moment

Tell from the filesystem and project history which this is.

- **First run ever** (no `projects/` folder, no `serve_media.log`, little or no
  history): give the fuller orientation. What Studio is (screencast and
  podcast projects), what's possible right now, what's missing and what it
  would unlock.
- **Routine cold start** (projects exist, the server just isn't running): start
  it, say "server's up," mention only anything newly broken, and stop.
