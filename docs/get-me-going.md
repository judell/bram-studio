# Get me going

Playbook for any coding agent with shell access in this repo, when the user
says "Get me going." The README is the reference; this is the procedure.

## 0. Check what's here

Run `./check.sh`. It lists each piece Studio uses as `ok`, `missing` or
`optional`, with what it's for, and installs nothing. Work from its report,
not from your own reconstruction: it checks things you can't see (a stub
`python3` that would open a macOS dialog instead of running).

**If Apple's Command Line Tools are missing, start there.** Nothing else
runs without them, `./start.sh` included (it refuses, and says so).
Homebrew's installer brings them, so do the Homebrew walk-through in step 3
first, then come back to step 1.

## 1. Start the server

Run `./start.sh`. It starts `serve_media.py` in the background, or reports that
it is already running. If it says the server is running code older than the
files on disk, suggest `./start.sh --restart`. Never restart while a narration,
render or export is under way (`start.sh` refuses itself in that case).

`start.sh` prints the port it's on: 8765, or the next free one when
another checkout's Studio holds it. The port is also recorded in
`studio-port.json`.

The editor tab in the user's browser recovers on its own once the server is
up. Say so; don't walk the user through reloading.

## 2. Inventory by tier, report as needed

Check every tier below, but **say only what gates the user's likely next
step**: the core tier, and transcription (both genres want it early). One
or two sentences — what works now, and the first missing thing they'd
notice — never a wall of everything absent.

- The optional podcast tiers (cut-edge alignment, speaker matching) come
  up when the user starts a podcast project or asks; the features
  themselves flag their absence in context.
- Never volunteer the `remix_take.py --regate` / VAD tier; it matters only
  to someone already using that script.
- State a download's size when proposing that download, not in the survey.

The full map — for answering, not for reciting:

- **Core screencast** (cut scenes, pauses, callouts, final video): `ffmpeg`;
  Studio's Python (the venv at `~/.local/share/bram-studio/venv`) with
  Pillow.
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

`ffmpeg` and `whisper-cli` come from Homebrew: `brew install ffmpeg
whisper-cpp`.

Studio's Python is a venv of its own, which `start.sh` runs the server with
when it exists. Pillow goes there (not into Homebrew's or Apple's Python),
and later the podcast models' packages go there too:

```sh
python3 -m venv ~/.local/share/bram-studio/venv
~/.local/share/bram-studio/venv/bin/pip install Pillow
```

The whisper model (about 466 MB):

```sh
mkdir -p ~/.local/share/whisper-models
curl -L -o ~/.local/share/whisper-models/ggml-small.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
```

For podcasts, `ggml-large-v3-turbo.bin` (about 1.6 GB, same folder, same
source) transcribes more accurately; Studio uses it when it's there. Offer
it when the user starts a podcast, not before.

## 3. Offer installs, confirm first

Assume the user is not a developer. Many won't know what a terminal,
Homebrew or a command line is, and shouldn't need to. Explain each step in
plain words, say what it's for in Studio's terms ("so Studio can cut
video"), and never leave them at a dead end.

For anything missing that the user would want: show the commands and the
download sizes (where the README doesn't give one, check with
`curl -sIL <url> | grep -i content-length` before quoting), ask once, then
run and report what happened. Never install unasked.

**After installing anything, run `./start.sh --restart`** (it refuses while
a narration, render or export is under way), so the server picks up the
new tools, **then `./check.sh` again**, and tell the user what changed in
Studio's terms ("Studio can now transcribe").

### When Homebrew isn't installed

Check with `command -v brew`, and also look for `/opt/homebrew/bin/brew`
(Apple Silicon) and `/usr/local/bin/brew` (Intel). Your shell's `PATH` may
not include them even when Homebrew is installed. Many Mac users don't
have Homebrew, and without it the core tier can't be installed, so this
case matters.

**You can't install Homebrew yourself:** its installer asks for the user's
Mac password, which you must never ask for, type or handle. The user runs
it. Walk them through it like this, in your own words:

1. Say what it is and why: "Homebrew is a free, widely used tool for
   installing software on a Mac. Studio needs it to get the video tool it
   relies on (ffmpeg). Installing it takes about 5–15 minutes, and it also
   installs Apple's Command Line Tools (about 2.4 GB), which Studio's
   narration recorder needs too."
2. Tell them how to open Terminal: press ⌘ Space, type **Terminal**, press
   Return. A window with a text prompt opens.
3. Give them the command to paste, exactly as published at
   [brew.sh](https://brew.sh) (point them there so they can check it
   themselves):

   ```sh
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

   Paste it into Terminal and press Return.
4. Warn them what they'll see:
   - It asks for their **Mac login password**. As they type, **nothing
     appears on screen, not even dots**. That's normal. Type it and press
     Return.
   - It may say "Press RETURN to continue". Press Return.
   - It runs for several minutes with a lot of text. That's normal.
5. When it finishes, it prints **"Next steps"** with one or two commands
   to run (on Apple Silicon Macs, a line containing
   `eval "$(/opt/homebrew/bin/brew shellenv)"`). Tell them to copy and run
   those lines too, the same way. Without them, the `brew` command isn't
   found in new Terminal windows.
6. Ask them to come back here and say "done" (or paste any error they saw).

Then pick up where you left off. Find `brew` by its full path
(`/opt/homebrew/bin/brew`, or `/usr/local/bin/brew` on Intel) rather than
trusting your shell's `PATH`, which was set before Homebrew existed. Then
install, confirm first as above: `brew install ffmpeg whisper-cpp`, then
Studio's Python and the whisper model (the commands in step 2), then
restart the server and re-check, as above.

If the installer fails, read the error they paste. The two common causes
are no internet connection and an account that isn't an administrator on
the Mac. For the second, say plainly that someone with an admin account
needs to install Homebrew, or make their account an admin in System
Settings › Users & Groups.

## 4. Match the moment

Tell from the filesystem and project history which this is.

- **First run ever** (no `projects/` folder, no `serve_media.log`, little or no
  history): give the fuller orientation. What Studio is (screencast and
  podcast projects), what's possible right now, what's missing and what it
  would unlock.
- **Routine cold start** (projects exist, the server just isn't running): start
  it, say "server's up," mention only anything newly broken, and stop.
