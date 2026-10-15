#!/usr/bin/env bash
# Lunapad installer for Raspberry Pi OS. Run as root, straight from GitHub:
#     curl -fsSL https://raw.githubusercontent.com/NSF12345/lunapad/main/install.sh | sudo bash
# or from a checkout of the repo:
#     sudo ./install.sh
# Piped in, it first clones the repo (LUNAPAD_REPO, default the public one) into a
# temporary folder and runs itself from there.
# Safe to re-run: it brings everything back to the installed state ("repair").
#
#   - apt packages: python3-pygame, fbi, bluez, git
#   - a `lunapad` account (no password) that logs in on the TV console and runs
#     the launcher; it can reboot / run apt updates via sudo, and nothing else
#   - the code in /opt/lunapad (a git checkout, owned by root)
#   - settings, cache and Moonlight's pairing live in /home/lunapad
#
# Moonlight Qt itself is NOT installed - see the README.
# --update: what deploy/self-update.sh runs after checking out new code -
# packages, the code's BUILD, sudo rules, login loop and apt config, but no
# account, autologin or restart changes (the launcher restarts itself).
# Environment: LUNAPAD_REF=<tag/branch/commit> to install something other than
# the latest release tag (or the source's current commit if there are no tags).
# LUNAPAD_HOST=<hostname or IP> presets the gaming PC for a new install
# (otherwise Lunapad asks on first start; existing settings are never changed).
set -euo pipefail

# Everything runs inside main(), called on the last line: piped from curl, a
# download cut short never runs, and a full one is read completely first.
main() {
APP_USER=lunapad
APP_HOME=/home/$APP_USER
APP_DIR=/opt/lunapad
UPDATE=0; [ "${1:-}" = "--update" ] && UPDATE=1

say() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./install.sh, or ... | sudo bash"; exit 1; }

if [ ! -f "${BASH_SOURCE[0]:-}" ]; then   # piped in (curl ... | sudo bash): fetch the repo, run from there
    local repo="${LUNAPAD_REPO:-https://github.com/NSF12345/lunapad.git}" tmp rc=0
    if ! command -v git >/dev/null; then
        say "Installing git"
        apt-get update -q
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q git
    fi
    tmp="$(mktemp -d)"
    say "Fetching $repo"
    git clone --quiet "$repo" "$tmp/lunapad"
    bash "$tmp/lunapad/install.sh" "$@" </dev/null || rc=$?
    rm -rf "$tmp"
    exit "$rc"
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SRC/lunapad.py" ] || { echo "Run this from a Lunapad checkout ($SRC has no lunapad.py)"; exit 1; }

say "Packages"
[ "$UPDATE" = 1 ] || apt-get update -q   # an update skips this: the daily update check keeps the lists fresh
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-pygame fbi bluez git

if [ "$UPDATE" = 0 ]; then
say "Account: $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --create-home --home-dir "$APP_HOME" --shell /bin/bash --comment "Lunapad" "$APP_USER"
    echo "Created user $APP_USER"
fi
passwd -l "$APP_USER" >/dev/null   # no password: it only ever logs in automatically on tty1
for g in video render input audio bluetooth tty; do   # display, GPU, controllers, sound, Bluetooth, console
    getent group "$g" >/dev/null && usermod -aG "$g" "$APP_USER"
done
id "$APP_USER"
fi

say "Code: $APP_DIR"
git_() { git -c safe.directory='*' "$@"; }   # the source checkout may belong to another user
if [ "$SRC" != "$APP_DIR" ]; then
    if [ -d "$APP_DIR/.git" ]; then
        git_ -C "$APP_DIR" fetch --tags --force "$SRC" '+refs/heads/*:refs/remotes/source/*'
    else
        rm -rf "$APP_DIR"
        git_ clone --quiet "$SRC" "$APP_DIR"
        git_ -C "$APP_DIR" remote rename origin source
    fi
fi
# Default: the newest release (the highest vX.Y.Z tag, as the Stable channel picks), else the source's commit.
LATEST="$(git_ -C "$APP_DIR" tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)"
REF="${LUNAPAD_REF:-${LATEST:-$(git_ -C "$SRC" rev-parse HEAD)}}"
git_ -C "$APP_DIR" checkout --quiet --force "$REF"
# A self-update to anything but a release tag came from the beta channel - say so in the title.
CHANNEL=""
if [ "$UPDATE" = 1 ] && ! git_ -C "$APP_DIR" tag --points-at HEAD | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' >/dev/null; then
    CHANNEL="beta "
fi
echo "$CHANNEL$(git_ -C "$APP_DIR" describe --tags --always) $(date +%Y-%m-%d)" > "$APP_DIR/BUILD"
chown -R root:root "$APP_DIR"
chmod -R a+rX,go-w "$APP_DIR"
echo "Installed $(cat "$APP_DIR/BUILD")"

say "Boot loop, sudo rules, apt config"
# From the installed version, not the source: they must match the code in $APP_DIR.
# Anything in $APP_HOME belongs to the lunapad account, so write there AS that account:
# done as root, a symlink it planted could redirect the write or the chown.
runuser -u "$APP_USER" -- install -m 0644 "$APP_DIR/deploy/bash_profile" "$APP_HOME/.bash_profile"
sudoers_tmp="$(mktemp)"   # private (0600), not a fixed /tmp name another account could plant
sed -e "s|__USER__|$APP_USER|g" -e "s|__APPDIR__|$APP_DIR|g" "$APP_DIR/deploy/sudoers-lunapad" > "$sudoers_tmp"
visudo -cf "$sudoers_tmp"   # a broken sudoers file can lock out sudo - check first
install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/lunapad
rm -f "$sudoers_tmp"
install -o root -g root -m 0644 "$APP_DIR/deploy/apt-lunapad.conf" /etc/apt/apt.conf.d/90lunapad-noprompt

if [ "$UPDATE" = 1 ]; then
    echo "Done. Lunapad $(cat "$APP_DIR/BUILD") is installed; the launcher restarts into it."
    exit 0
fi

CONFIG="$APP_HOME/.config/lunapad/lunapad.toml"
if [ -n "${LUNAPAD_HOST:-}" ]; then
    if [ -e "$CONFIG" ] || [ -L "$CONFIG" ]; then
        echo "LUNAPAD_HOST ignored: settings already exist ($CONFIG)"
    elif [[ "$LUNAPAD_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
        runuser -u "$APP_USER" -- mkdir -p "$(dirname "$CONFIG")"
        printf '[host]\naddress = "%s"\n' "$LUNAPAD_HOST" \
            | runuser -u "$APP_USER" -- tee "$CONFIG" >/dev/null   # the rest fills in with defaults
        echo "Host set to $LUNAPAD_HOST"
    else
        echo "LUNAPAD_HOST ignored: '$LUNAPAD_HOST' isn't a hostname or IP - set it up on the TV instead"
    fi
fi

say "Console autologin as $APP_USER (tty1)"
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $APP_USER --noclear %I \$TERM
EOF
systemctl daemon-reload

say "Starting Lunapad as $APP_USER"
pkill -f '[l]unapad/lunapad.py' || true   # whatever runs on tty1 now
systemctl restart getty@tty1             # autologin -> the new boot loop
echo "Done. Lunapad $(cat "$APP_DIR/BUILD") is starting on the TV."
}

main "$@"
