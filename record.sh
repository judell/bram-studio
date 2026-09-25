#!/bin/bash
# Record one take: voice until Stop -> render -> register.
#   ./record.sh [source]   (started by serve_media.py's POST /record)
#
# source is a movie (usually a symlink) in sources/; with none given, the
# only one there is used. The app plays it in a MediaPlayer and logs the
# player's events; its Stop button posts them (POST /record/stop), which
# writes <session>/events.json and then media/.record-stop, ending the take.
#
# The voice comes from record_native (AVFoundation's own file writer), not
# ffmpeg's capture, which drops ~10% of samples (see voicetest.py). render.py
# rebuilds the picture from the events, and the take gets the raw voice
# slice, since its leveling sounds worse.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
# Sessions, voice slices and renders live in work/ (gitignored).
WORK="$HERE/work"
MIC="${MIC:-MacBook Air Microphone}"
NATIVE="$HOME/.cache/bram-studio/record_native"
# The phase serve_media.py reports at /record/status, for the app's spinner.
STATE="$HERE/media/.record-state"
phase() { echo "$1" > "$STATE"; }
SESSION_FILE="$HERE/media/.record-session"
STOP="$HERE/media/.record-stop"
# Restart / Discard (POST /record/restart, /record/cancel): end without a take.
CANCEL="$HERE/media/.record-cancel"
trap 'rm -f "$STATE" "$SESSION_FILE" "$STOP" "$CANCEL"' EXIT
rm -f "$STOP" "$CANCEL"
phase starting
if [ -n "$1" ]; then
  SRC="$1"
else
  shopt -s nullglob
  srcs=("$HERE"/sources/*.mp4 "$HERE"/sources/*.mov "$HERE"/sources/*.m4v)
  if [ ${#srcs[@]} -ne 1 ]; then echo "pass a source: ${#srcs[@]} movies in $HERE/sources"; exit 1; fi
  SRC="${srcs[0]}"
fi
SRC=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$SRC")
[ -f "$SRC" ] || { echo "no such movie: $SRC"; exit 1; }
if [ ! -x "$NATIVE" ] || [ "$HERE/record_native.swift" -nt "$NATIVE" ]; then
  mkdir -p "$(dirname "$NATIVE")"
  swiftc -O -o "$NATIVE" "$HERE/record_native.swift"
fi
mkdir -p "$WORK/sessions"
cd "$WORK"

# Session: native voice recording; the picture comes from the app's events.
dir="sessions/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$dir"
"$NATIVE" "$MIC" "$dir/voice.wav" 0 > "$dir/recorder.out" 2> "$dir/recorder.err" &
rec=$!
echo "$rec" > "$dir/pids"
for _ in $(seq 100); do grep -q '^started' "$dir/recorder.out" && break; sleep 0.1; done
t0=$(awk '/^started/ {print $2}' "$dir/recorder.out")
if [ -z "$t0" ]; then
  echo "recorder never started; see $WORK/$dir/recorder.err"
  kill "$rec" 2>/dev/null || true
  exit 1
fi
echo "$t0" > "$dir/t0"
echo "$WORK/$dir" > "$SESSION_FILE"
echo "recording in $WORK/$dir; click Stop in the app to finish"
phase recording

while [ ! -f "$STOP" ] && [ ! -f "$CANCEL" ]; do sleep 0.2; done
if [ -f "$CANCEL" ]; then
  kill -TERM "$rec" 2>/dev/null || true
  wait "$rec" || true
  # Kept, not deleted: the voice is still there if a discard was a mistake.
  touch "$dir/cancelled"
  echo "cancelled $WORK/$dir (no take)"
  exit 0
fi
closed=$(python3 -c 'import os, sys; print(f"{os.path.getmtime(sys.argv[1]):.3f}")' "$STOP")
phase rendering
kill -TERM "$rec" 2>/dev/null || true
wait "$rec" || true
# render.py reads raw s16le by byte offset; with no gaps the offsets are exact.
ffmpeg -v error -y -i "$dir/voice.wav" -ac 1 -ar 48000 -f s16le -acodec pcm_s16le "$dir/audio.pcm"

# The take runs from the recorder's first sample (go + render.py's 0.2s GAP = 0)
# to Stop.
go=-0.2
stop=$(python3 -c "print($closed - $t0 + 0.2)")
python3 "$HERE/render.py" "$dir" "$go" "$stop" "$SRC" --events "$dir/events.json"
# render.py names the take after its session, so names never repeat.
take="take-$(basename "$dir")"
json="takes/$take.json"

# Swap render.py's leveled voice for the raw slice it saved.
ffmpeg -v error -y -i "narrated/$take.mp4" -i "takes/$take.wav" -map 0:v -map 1:a -c:v copy \
  -af "afade=t=in:d=0.1" -c:a aac -b:a 160k -shortest "narrated/$take-raw.mp4"
# A take with no player events and no ink (strokes or pointing) renders as a
# still frame: flag it, with what the player reported at Stop (diag.json), so
# the cause can be told apart later. Ink over a still frame is a legit take.
warning=""
if [ "$(jq '[.[] | select(.event == "play" or .event == "pause" or .event == "seeked" or .event == "ended" or .event == "stroke" or .event == "shape" or .event == "pointer")] | length' "$dir/events.json")" = "0" ]; then
  warning="no player events; $(jq -r '"duration=\(.duration // "none"), paused=\(.paused), currentTime=\(.currentTime)"' "$dir/diag.json" 2>/dev/null || echo "no diag")"
fi
# "-": register.py names the take from its first spoken words.
phase naming
python3 "$HERE/register.py" "narrated/$take-raw.mp4" - \
  "$(jq -r .src_start "$json")" "$(jq -r .src_end "$json")" "$(basename "$SRC")" "$warning"
