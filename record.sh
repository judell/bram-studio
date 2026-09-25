#!/bin/bash
# Record one take: QuickTime session -> close -> render -> register.
#   ./record.sh          (then narrate; close the QuickTime window to finish)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
VT="$HOME/Desktop/video-test"
cd "$VT"
dir=$(./studio.sh | awk '{print $1}')
echo "recording in $VT/$dir; close the QuickTime window to finish"
closed=$(./closewatch.sh | awk '/^CLOSED/ {print $2}')
kill $(cat "$dir/pids") 2>/dev/null || true
t0=$(cat "$dir/t0"); opened=$(cat "$dir/opened")
go=$(python3 -c "print($opened - $t0 - 0.2)")
stop=$(python3 -c "print($closed - $t0 + 0.2)")
python3 take.py "$dir" "$go" "$stop"
json=$(ls -t takes/perf-*.json | head -1)
take=$(jq -r .take "$json")
python3 "$HERE/register.py" "narrated/$take.mp4" "$take" \
  "$(jq -r .src_start "$json")" "$(jq -r .src_end "$json")"
