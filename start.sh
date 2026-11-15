#!/bin/sh
# Start Studio's server (serve_media.py), detached, logging to
# serve_media.log. Bram doesn't start it: a .bram.json "server" entry would
# also become the app's address, and Studio's page comes from Bram's own
# server.
#
#   ./start.sh            start it, or say it's running (and whether it's
#                         running code older than the files on disk)
#   ./start.sh --restart  stop it and start it again, unless a narration,
#                         render or export is under way
#
# Each checkout's server has its own port: 8765, or the next free one when
# another checkout's Studio holds it (per-checkout-server-port). The server
# records its choice in studio-port.json; this script finds this checkout's
# server there (8765 when there's no file, which also finds a server started
# before ports varied). Only this checkout's serve_media.py is ever stopped.
cd "$(dirname "$0")" || exit 1
DIR=$(pwd)

# On a fresh Mac (start-sh-fresh-mac):
# - Without Apple's Command Line Tools, /usr/bin/python3 is a stub that opens
#   a macOS install dialog instead of running, so say so plainly first.
# - The server runs ffmpeg and whisper-cli by name and inherits this PATH,
#   which may predate a Homebrew install (an agent's shell, say): put
#   Homebrew's folders first so tools installed a minute ago are found.
# - The server runs on Studio's own Python, the venv the speech models
#   already use, so Pillow and the models share one interpreter. Without the
#   venv, plain python3 (Pillow, if missing, only fails at the first render).
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Studio needs Apple's Command Line Tools, which aren't installed." >&2
  echo "Run ./check.sh, and see docs/get-me-going.md for how to install them." >&2
  exit 1
fi
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
VENV_PY="$HOME/.local/share/bram-studio/venv/bin/python"
if [ -x "$VENV_PY" ]; then PY="$VENV_PY"; else PY=python3; fi

recorded_port() { sed -n 's/.*"port": *\([0-9][0-9]*\).*/\1/p' studio-port.json 2>/dev/null; }
PORT=$(recorded_port)
PORT=${PORT:-8765}
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
  # The server picks its port and writes studio-port.json once it holds it;
  # a file left by a server that's gone would name the wrong port, so it
  # goes first.
  rm -f studio-port.json
  nohup "$PY" serve_media.py >> serve_media.log 2>&1 < /dev/null &
  i=0
  while [ $i -lt 30 ]; do
    PORT=$(recorded_port)
    if [ -n "$PORT" ] && curl -s -o /dev/null "http://127.0.0.1:$PORT/sources"; then
      echo "Studio's server is running on port $PORT (PID $(listener)), logging to serve_media.log."
      return 0
    fi
    sleep 0.5; i=$((i + 1))
  done
  echo "Studio's server didn't answer within 15 s; see the end of serve_media.log." >&2
  return 1
}

pid=$(listener)
if [ -z "$pid" ] || ! ours "$pid"; then
  # Nothing on this checkout's port, or another checkout's server took it
  # after ours went away: start ours, on whatever port is free.
  start; exit $?
fi

if [ "$1" != "--restart" ]; then
  echo "Studio's server is already running on port $PORT (PID $pid, since $(lstart "$pid"))."
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
