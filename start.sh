#!/bin/sh
# Start Studio's server (serve_media.py, port 8765), detached, logging to
# serve_media.log. Bram doesn't start it: a .bram.json "server" entry would
# also become the app's address, and Studio's page comes from Bram's own
# server.
#
#   ./start.sh            start it, or say it's running (and whether it's
#                         running code older than the files on disk)
#   ./start.sh --restart  stop it and start it again, unless a narration,
#                         render or export is under way
#
# Only a server on 8765 that is this checkout's serve_media.py is stopped;
# anything else on the port is reported and left alone.
cd "$(dirname "$0")" || exit 1
DIR=$(pwd)
PORT=8765
URL=http://127.0.0.1:$PORT

listener() { lsof -nP -t -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | head -1; }

ours() {
  # This checkout's serve_media.py: by absolute path, or by name when run
  # from this folder (a project switch restarts it in place, same PID).
  cmd=$(ps -o command= -p "$1")
  case "$cmd" in
    *"$DIR/serve_media.py"*) return 0 ;;
    *serve_media.py*)
      cwd=$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
      [ "$cwd" = "$DIR" ] && return 0 ;;
  esac
  return 1
}

lstart() { ps -o lstart= -p "$1" | sed 's/ *$//'; }
started() { date -j -f "%a %b %d %T %Y" "$(lstart "$1")" +%s; }

start() {
  nohup python3 serve_media.py >> serve_media.log 2>&1 < /dev/null &
  i=0
  while [ $i -lt 30 ]; do
    if curl -s -o /dev/null "$URL/sources"; then
      echo "Studio's server is running (PID $(listener)), logging to serve_media.log."
      return 0
    fi
    sleep 0.5; i=$((i + 1))
  done
  echo "Studio's server didn't answer within 15 s; see the end of serve_media.log." >&2
  return 1
}

pid=$(listener)
if [ -z "$pid" ]; then
  start; exit $?
fi
if ! ours "$pid"; then
  echo "Port $PORT is held by something else, left alone:" >&2
  ps -o pid,lstart,command -p "$pid" >&2
  exit 1
fi

if [ "$1" != "--restart" ]; then
  echo "Studio's server is already running (PID $pid, since $(lstart "$pid"))."
  since=$(started "$pid")
  for f in serve_media.py studio_paths.py overlay.py; do
    if [ -f "$f" ] && [ "$(stat -f %m "$f")" -gt "$since" ]; then
      echo "$f changed after it started, so it's running older code: ./start.sh --restart"
      break
    fi
  done
  exit 0
fi

# --restart: not while something is under way.
if ! curl -s "$URL/narrate/status" | grep -q '"id": null'; then
  echo "Not restarting: a narration is under way (or /narrate/status didn't answer)." >&2
  exit 1
fi
kids=$(pgrep -P "$pid")
if [ -n "$kids" ]; then
  echo "Not restarting: the server is running something (a render, export or the recorder):" >&2
  ps -o pid,command -p "$(echo $kids | tr ' ' ',')" >&2
  exit 1
fi
kill "$pid"
i=0
while [ -n "$(listener)" ] && [ $i -lt 40 ]; do sleep 0.25; i=$((i + 1)); done
if [ -n "$(listener)" ]; then
  echo "The old server (PID $pid) didn't stop; not starting another." >&2
  exit 1
fi
start
