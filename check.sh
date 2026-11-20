#!/bin/sh
# What Studio needs, and what's here: a read-only report, one line per piece,
# "ok" or "missing" with what it's for. Installs nothing. docs/get-me-going.md
# has the agent run it first and branch on what it says (start-sh-fresh-mac).
#
# It never runs the python3 stub: without Apple's Command Line Tools,
# /usr/bin/python3 opens a macOS install dialog instead of running, so the
# Python checks wait until the tools are known to be there.
cd "$(dirname "$0")" || exit 1

VENV="$HOME/.local/share/bram-studio/venv"
WHISPER="$HOME/.local/share/whisper-models"
SPEECH="$HOME/.local/share/speech-models"
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"  # as start.sh gives the server
missing=0

ok()   { printf '  ok       %s\n' "$1"; }
miss() { printf '  missing  %s: %s\n' "$1" "$2"; missing=$((missing + 1)); }
opt()  { printf '  optional %s: %s\n' "$1" "$2"; }

echo "Needed for anything:"
if xcode-select -p >/dev/null 2>&1; then
  ok "Apple's Command Line Tools ($(xcode-select -p))"
  clt=1
else
  miss "Apple's Command Line Tools" "Python and the narration recorder; Homebrew's installer brings them"
  clt=0
fi
if [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ]; then
  ok "Homebrew ($(command -v brew))"
else
  miss "Homebrew" "the usual way to install ffmpeg and whisper"
fi
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg ($(command -v ffmpeg))"
else
  miss "ffmpeg" "cutting and joining video; brew install ffmpeg"
fi
if [ "$clt" = 1 ] && [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import PIL" 2>/dev/null; then
  ok "Studio's Python, with Pillow ($VENV)"
elif [ "$clt" = 1 ] && [ -x "$VENV/bin/python" ]; then
  miss "Pillow in Studio's Python" "drawing callouts; $VENV/bin/pip install Pillow"
else
  miss "Studio's Python ($VENV)" "runs the server and draws callouts; python3 -m venv $VENV, then pip install Pillow"
fi

echo "Naming takes and transcription:"
if command -v whisper-cli >/dev/null 2>&1; then
  ok "whisper-cli ($(command -v whisper-cli))"
else
  miss "whisper-cli" "turning speech into text; brew install whisper-cpp"
fi
if [ -f "$WHISPER/ggml-large-v3-turbo.bin" ]; then
  ok "whisper model, large-v3-turbo"
elif [ -f "$WHISPER/ggml-small.en.bin" ]; then
  ok "whisper model, small.en"
  opt "whisper model, large-v3-turbo" "more accurate podcast transcripts (about 1.6 GB)"
else
  miss "whisper model" "transcription; ggml-small.en.bin in $WHISPER (about 466 MB)"
fi

echo "Optional, for podcasts:"
if [ -d "$SPEECH/sherpa-onnx-nemo-ctc-en-conformer-small" ] && [ "$clt" = 1 ] \
   && [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import sherpa_onnx" 2>/dev/null; then
  ok "cut-edge alignment"
else
  opt "cut-edge alignment" "cuts land in the quiet between words; without it, whisper's word times"
fi
if [ -d "$SPEECH/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8" ]; then
  ok "Parakeet model"
else
  opt "Parakeet model" "second opinions from NVIDIA Parakeet (about 700 MB; docs/get-me-going.md)"
fi
if [ -f "$SPEECH/nemo_en_titanet_small.onnx" ]; then
  ok "speaker matching model"
else
  opt "speaker matching model" "Final transcript says who's speaking"
fi

echo "Starting with Bram:"
# Whether Bram runs ./start.sh at each launch (.bram.json launch.run): it
# waits for an Allow on its banner, once per computer (issue 4). Bram
# reports the decision at GET /__launch-run, on the port in .bram/.bram-port.
# Nothing is printed when Bram isn't running or is too old to have the
# route; it never counts as missing, since ./start.sh works by hand.
BRAM_PORT=$(cat .bram/.bram-port 2>/dev/null)
if [ -n "$BRAM_PORT" ]; then
  lr=$(curl -4 -sS --max-time 2 "http://127.0.0.1:$BRAM_PORT/__launch-run" 2>/dev/null)
  case "$lr" in
    *'"command"'*)
      case "$lr" in
        *'"decision":"allow"'*'"runAtLaunch":false'*|*'"runAtLaunch":false'*'"decision":"allow"'*|*'"decision":"deny"'*)
          opt "Bram runs ./start.sh at launch" "turned off; Settings › Companion App turns it back on" ;;
        *'"decision":"allow"'*)
          ok "Bram runs ./start.sh at each launch" ;;
        *)
          printf '  waiting  %s: %s\n' "Bram's Allow for ./start.sh" "click Allow on Bram's banner, or Studio's server won't start at the next launch" ;;
      esac ;;
  esac
fi

echo
if [ "$missing" = 0 ]; then
  echo "Everything Studio needs is here."
else
  echo "$missing needed piece(s) missing. docs/get-me-going.md says how to install each."
fi
