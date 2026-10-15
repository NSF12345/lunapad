#!/usr/bin/env bash
# Updates Lunapad in place from the public repo, to the newest stable release
# (the highest vX.Y.Z tag) or the tip of the beta branch, then runs the new
# code's install.sh --update (packages, sudo rules, login loop, apt config).
# The launcher's Update screen runs it as root (see deploy/sudoers-lunapad):
#     sudo /bin/bash /opt/lunapad/deploy/self-update.sh stable|beta
# Everything is inside main() because bash reads a script while running it, and
# the checkout below replaces this very file.
set -euo pipefail

main() {
    local channel="${1:-}" app_dir repo ref
    app_dir="$(cd "$(dirname "$0")/.." && pwd)"
    repo="${LUNAPAD_REPO:-https://github.com/NSF12345/lunapad.git}"
    git_() { git -c safe.directory='*' -C "$app_dir" "$@"; }
    [ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }
    case "$channel" in stable|beta) ;; *) echo "Usage: self-update.sh stable|beta"; exit 2 ;; esac

    if git_ remote get-url origin >/dev/null 2>&1; then
        git_ remote set-url origin "$repo"
    else
        git_ remote add origin "$repo"
    fi
    echo "Fetching $repo"
    GIT_TERMINAL_PROMPT=0 git_ fetch --tags --force origin '+refs/heads/*:refs/remotes/origin/*'

    if [ "$channel" = stable ]; then
        ref="$(git_ tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)"
        [ -n "$ref" ] || { echo "No stable release yet"; exit 1; }
    else
        ref=origin/beta
    fi
    ref="$(git_ rev-parse --verify --quiet "$ref^{commit}")" || { echo "$channel not found in $repo"; exit 1; }
    echo "Checking out $channel: $(git_ describe --tags --always "$ref")"
    git_ checkout --quiet --force "$ref"
    LUNAPAD_REF="$ref" exec bash "$app_dir/install.sh" --update
}

main "$@"
exit
