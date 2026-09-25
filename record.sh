#!/bin/bash
# Record one take: QuickTime session -> close -> render -> register.
#   ./record.sh [source]   (then narrate; close the QuickTime window to finish)
#
# source is a movie (usually a symlink) in sources/; with none given, the
# only one there is used.
#
# The voice comes from record_native (AVFoundation's own file writer), not
# session.sh's ffmpeg capture, which drops ~10% of samples (see voicetest.py).
# ~/Desktop/video-test's playhead.js and closewatch.sh run unchanged; render.py
# (take.py with the source as an argument) renders, and the take gets the raw
# voice slice, since its leveling sounds worse.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
VT="$HOME/Desktop/video-test"
MIC="${MIC:-MacBook Air Microphone}"
NATIVE="$HOME/.cache/bram-studio/record_native"
if [ -n "$1" ]; then
  SRC="$1"
else
  shopt -s nullglob
  srcs=("$HERE"/sources/*.mp4 "$HERE"/sources/*.mov "$HERE"/sources/*.m4v)
  if [ ${#srcs[@]} -ne 1 ]; then echo "pass a source: ${#srcs[@]} movies in $HERE/sources"; exit 1; fi
  SRC="${srcs[0]}"
fi
# QuickTime names the document after the real file, and render.py matches on it.
SRC=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$SRC")
[ -f "$SRC" ] || { echo "no such movie: $SRC"; exit 1; }
if [ ! -x "$NATIVE" ] || [ "$HERE/record_native.swift" -nt "$NATIVE" ]; then
  mkdir -p "$(dirname "$NATIVE")"
  swiftc -O -o "$NATIVE" "$HERE/record_native.swift"
fi
cd "$VT"

# Session: playhead logger + native voice recording (as session.sh did).
osascript -e 'if application "QuickTime Player" is running then tell application "QuickTime Player" to quit' >/dev/null
sleep 1
dir="sessions/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$dir"
osascript -l JavaScript playhead.js 2>> playhead.log >/dev/null &
logger=$!
"$NATIVE" "$MIC" "$dir/voice.wav" 0 > "$dir/recorder.out" 2> "$dir/recorder.err" &
rec=$!
printf '%s\n%s\n' "$logger" "$rec" > "$dir/pids"
for _ in $(seq 100); do grep -q '^started' "$dir/recorder.out" && break; sleep 0.1; done
t0=$(awk '/^started/ {print $2}' "$dir/recorder.out")
if [ -z "$t0" ]; then
  echo "recorder never started; see $VT/$dir/recorder.err"
  kill "$logger" "$rec" 2>/dev/null || true
  exit 1
fi
echo "$t0" > "$dir/t0"

open -a "QuickTime Player" "$SRC"
opened=$(python3 -c 'import time; print(f"{time.time():.3f}")')
echo "$opened" > "$dir/opened"
echo "recording in $VT/$dir; close the QuickTime window to finish"

closed=$(./closewatch.sh | awk '/^CLOSED/ {print $2}')
kill "$logger" 2>/dev/null || true
kill -TERM "$rec" 2>/dev/null || true
wait "$rec" || true
# render.py reads raw s16le by byte offset; with no gaps the offsets are exact.
ffmpeg -v error -y -i "$dir/voice.wav" -ac 1 -ar 48000 -f s16le -acodec pcm_s16le "$dir/audio.pcm"

go=$(python3 -c "print($opened - $t0 - 0.2)")
stop=$(python3 -c "print($closed - $t0 + 0.2)")
python3 "$HERE/render.py" "$dir" "$go" "$stop" "$SRC"
json=$(ls -t takes/perf-*.json | head -1)
take=$(jq -r .take "$json")

# Swap render.py's leveled voice for the raw slice it saved.
ffmpeg -v error -y -i "narrated/$take.mp4" -i "takes/$take.wav" -map 0:v -map 1:a -c:v copy \
  -af "afade=t=in:d=0.1" -c:a aac -b:a 160k -shortest "narrated/$take-raw.mp4"
# "-": register.py names the take from its first spoken words.
python3 "$HERE/register.py" "narrated/$take-raw.mp4" - \
  "$(jq -r .src_start "$json")" "$(jq -r .src_end "$json")" "$(basename "$SRC")"
