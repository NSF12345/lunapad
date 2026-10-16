#!/usr/bin/env bash
# Installs (or repairs / updates) Lunapad on the Pi from this checkout's
# committed history: sends the repo as a git bundle and runs install.sh there
# as root. Uncommitted changes are NOT included - use deploy.sh to test those.
# Usage (Git Bash):  bash deploy/install-on-pi.sh      PI=user@host (or deploy/pi-target) for the Pi,
#                    LUNAPAD_REF=<tag/branch/commit> to install a specific version,
#                    LUNAPAD_HOST=<hostname or IP> to preset the gaming PC on a new install.
# Asks for the Pi password for the upload and the session, then for sudo.
set -euo pipefail
REF="${LUNAPAD_REF:-}"
HOST="${LUNAPAD_HOST:-}"
cd "$(dirname "$0")/.."
# The Pi to use: PI=user@host, or saved once in deploy/pi-target (git-ignored).
PI="${PI:-$(cat deploy/pi-target 2>/dev/null || true)}"
[ -n "$PI" ] || { echo "Which Pi? Run with PI=user@host, or save user@host in deploy/pi-target"; exit 1; }

[ -z "$(git status --porcelain)" ] || echo "NOTE: uncommitted changes are not included"
bundle="$(mktemp)"
git bundle create "$bundle" --all 2>/dev/null
echo "Sending $(git describe --tags --always) ($(du -k "$bundle" | cut -f1) KB) to $PI"
# A private folder (mktemp -d, mode 0700) on the Pi: fixed /tmp names could be
# swapped by another account before sudo runs install.sh from there.
remote="$(ssh "$PI" 'd="$(mktemp -d)" && cat > "$d/lunapad.bundle" && echo "$d"' < "$bundle")"
rm -f "$bundle"
case "$remote" in /tmp/*) ;; *) echo "FAIL: unexpected temp folder on the Pi: $remote"; exit 1 ;; esac

ssh -t "$PI" "
set -e
command -v git >/dev/null || { echo 'Installing git first...'; sudo apt-get install -y -q git; }
git clone --quiet '$remote/lunapad.bundle' '$remote/src'
sudo ${REF:+LUNAPAD_REF='$REF'} ${HOST:+LUNAPAD_HOST='$HOST'} bash '$remote/src/install.sh'
rm -rf '$remote'
"
