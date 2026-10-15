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
ssh "$PI" "cat > /tmp/lunapad.bundle" < "$bundle"
rm -f "$bundle"

ssh -t "$PI" "
set -e
command -v git >/dev/null || { echo 'Installing git first...'; sudo apt-get install -y -q git; }
rm -rf /tmp/lunapad-src
git clone --quiet /tmp/lunapad.bundle /tmp/lunapad-src
sudo ${REF:+LUNAPAD_REF='$REF'} ${HOST:+LUNAPAD_HOST='$HOST'} bash /tmp/lunapad-src/install.sh
rm -rf /tmp/lunapad-src /tmp/lunapad.bundle
"
