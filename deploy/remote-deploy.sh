#!/usr/bin/env bash
# Runs ON THE PI as root, from the private temp folder deploy.sh unpacked into.
# Copies the files into /opt/lunapad, restarts the launcher (the lunapad
# account's boot loop relaunches it) and verifies the new process.
set -euo pipefail
APP_DIR=/opt/lunapad
APP_USER=lunapad
cd "$(dirname "$0")"

[ -d "$APP_DIR" ] || { echo "FAIL: $APP_DIR missing - install first (deploy/install-on-pi.sh)"; exit 1; }
# compile everything before copying anything in, so a bad file changes nothing
for f in *.py; do python3 -m py_compile "$f"; done
rm -rf __pycache__
for f in lunapad.py settings.py bluetooth_icon.png splash.png; do
    install -o root -g root -m 0644 "$f" "$APP_DIR/$f"
done
install -o root -g root -m 0644 BUILD "$APP_DIR/BUILD"
ls -la --time-style=full-iso "$APP_DIR/lunapad.py" "$APP_DIR/BUILD"
mtime=$(stat -c %Y "$APP_DIR/lunapad.py")

# [l] keeps the pattern from matching this shell's own command line
pkill -9 -u "$APP_USER" -f '[l]unapad/lunapad.py' || true

# Wait (up to 30s) for the boot loop to start a Lunapad newer than the files.
# `|| true` matters: with pipefail, an empty pgrep would otherwise end the
# script silently instead of reaching the FAIL message.
pid=""
for _ in $(seq 30); do
    sleep 1
    cand=$(pgrep -u "$APP_USER" -f '[l]unapad/lunapad.py' | head -1 || true)
    [ -n "$cand" ] || continue
    start=$(( $(date +%s) - $(ps -o etimes= -p "$cand" | tr -d ' ') ))
    if [ "$start" -ge "$mtime" ]; then pid=$cand; break; fi
done
[ -n "$pid" ] || { echo "FAIL: no lunapad.py newer than the files is running as $APP_USER after 30s"; exit 1; }
ps -o pid,user,lstart,cmd -p "$pid"

sleep 5
pid2=$(pgrep -u "$APP_USER" -f '[l]unapad/lunapad.py' | head -1 || true)
if [ "$pid2" = "$pid" ]; then
    echo "OK: PID $pid started after the new files and is stable ($(cat "$APP_DIR/BUILD"))"
else
    echo "WARN: PID changed $pid -> ${pid2:-none} (kmsdrm race settles on its own; re-check if it keeps changing)"
    exit 1
fi
