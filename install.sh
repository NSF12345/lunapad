#!/usr/bin/env bash
# Lunapad installer for Raspberry Pi OS. Run as root from a checkout of the repo:
#     sudo ./install.sh
# Safe to re-run: it brings everything back to the installed state ("repair").
#
#   - apt packages: python3-pygame, fbi, bluez, git
#   - a `lunapad` account (no password) that logs in on the TV console and runs
#     the launcher; it can reboot / run apt updates via sudo, and nothing else
#   - the code in /opt/lunapad (a git checkout, owned by root)
#   - settings, cache and Moonlight's pairing live in /home/lunapad
#
# Moonlight Qt itself is NOT installed - see the README.
# Environment: LUNAPAD_REF=<tag/branch/commit> to install something other than
# the latest release tag (or the source's current commit if there are no tags).
# LUNAPAD_HOST=<hostname or IP> presets the gaming PC for a new install
# (otherwise Lunapad asks on first start; existing settings are never changed).
set -euo pipefail

APP_USER=lunapad
APP_HOME=/home/$APP_USER
APP_DIR=/opt/lunapad
SRC="$(cd "$(dirname "$0")" && pwd)"

say() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./install.sh"; exit 1; }
[ -f "$SRC/lunapad.py" ] || { echo "Run this from a Lunapad checkout ($SRC has no lunapad.py)"; exit 1; }

say "Packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-pygame fbi bluez git

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
REF="${LUNAPAD_REF:-$(git_ -C "$APP_DIR" describe --tags --abbrev=0 2>/dev/null || git_ -C "$SRC" rev-parse HEAD)}"
git_ -C "$APP_DIR" checkout --quiet --force "$REF"
echo "$(git_ -C "$APP_DIR" describe --tags --always) $(date +%Y-%m-%d)" > "$APP_DIR/BUILD"
chown -R root:root "$APP_DIR"
chmod -R a+rX,go-w "$APP_DIR"
echo "Installed $(cat "$APP_DIR/BUILD")"

say "Boot loop, sudo rules, apt config"
install -o "$APP_USER" -g "$APP_USER" -m 0644 "$SRC/deploy/bash_profile" "$APP_HOME/.bash_profile"
sed -e "s|__USER__|$APP_USER|g" -e "s|__APPDIR__|$APP_DIR|g" "$SRC/deploy/sudoers-lunapad" > /tmp/lunapad_sudoers
visudo -cf /tmp/lunapad_sudoers   # a broken sudoers file can lock out sudo - check first
install -o root -g root -m 0440 /tmp/lunapad_sudoers /etc/sudoers.d/lunapad
rm -f /tmp/lunapad_sudoers
install -o root -g root -m 0644 "$SRC/deploy/apt-lunapad.conf" /etc/apt/apt.conf.d/90lunapad-noprompt

CONFIG="$APP_HOME/.config/lunapad/lunapad.toml"
if [ -n "${LUNAPAD_HOST:-}" ]; then
    if [ -e "$CONFIG" ]; then
        echo "LUNAPAD_HOST ignored: settings already exist ($CONFIG)"
    elif [[ "$LUNAPAD_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
        install -d -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.config" "$APP_HOME/.config/lunapad"
        printf '[host]\naddress = "%s"\n' "$LUNAPAD_HOST" > "$CONFIG"   # the rest fills in with defaults
        chown "$APP_USER:$APP_USER" "$CONFIG"
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
