#!/usr/bin/env bash
# Dev deploy: copies this working copy's app files (uncommitted changes
# included) into the Pi's /opt/lunapad and restarts Lunapad, with the checks in
# CLAUDE.md. The next install-on-pi.sh run replaces them with committed code.
# Usage (Git Bash):  bash deploy/deploy.sh      (the Pi: PI=user@host or deploy/pi-target)
# Asks for the Pi password for the upload and the session, then for sudo.
set -euo pipefail
cd "$(dirname "$0")/.."
# The Pi to use: PI=user@host, or saved once in deploy/pi-target (git-ignored).
PI="${PI:-$(cat deploy/pi-target 2>/dev/null || true)}"
[ -n "$PI" ] || { echo "Which Pi? Run with PI=user@host, or save user@host in deploy/pi-target"; exit 1; }

# Git Bash, WSL and plain Linux name Python differently (and Windows has a
# python3 stub that just opens the Store) - use the first one that really runs.
PY=""
for p in python3 python py; do
    if "$p" -c "" >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -n "$PY" ]; then
    "$PY" -m py_compile lunapad.py settings.py
    echo "Local compile OK - deploying to $PI"
else
    echo "No local Python found - skipping local compile (the Pi still compiles it before copying)"
fi

stage="$(mktemp -d)"
cp lunapad.py settings.py bluetooth_icon.png splash.png deploy/remote-deploy.sh "$stage/"
echo "dev $(git describe --tags --always --dirty 2>/dev/null || echo unknown) $(date +%Y-%m-%d)" > "$stage/BUILD"
tar -czf "$stage.tgz" -C "$stage" .
# A private folder (mktemp -d, mode 0700) on the Pi: a fixed /tmp name could be
# swapped by another account before sudo runs remote-deploy.sh from there.
remote="$(ssh "$PI" 'd="$(mktemp -d)" && tar -xzf - -C "$d" && echo "$d"' < "$stage.tgz")"
rm -rf "$stage" "$stage.tgz"
case "$remote" in /tmp/*) ;; *) echo "FAIL: unexpected temp folder on the Pi: $remote"; exit 1 ;; esac

ssh -t "$PI" "sudo bash '$remote/remote-deploy.sh'; rc=\$?; rm -rf '$remote'; exit \$rc"
