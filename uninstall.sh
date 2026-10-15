#!/usr/bin/env bash
# Removes Lunapad - everything install.sh set up. Run as root on the Pi:
#     sudo /opt/lunapad/uninstall.sh [--keep-data] [--yes]
#
#   --keep-data  keep the `lunapad` account and its home (settings, cache,
#                Moonlight pairing), so a later install.sh picks them up again
#   --yes        don't ask for confirmation
#
# Not removed: the apt packages install.sh added (python3-pygame, fbi, bluez,
# git - other software may use them) and Moonlight Qt (never installed by Lunapad).
set -euo pipefail

APP_USER=lunapad
APP_DIR=/opt/lunapad
AUTOLOGIN=/etc/systemd/system/getty@tty1.service.d/autologin.conf
KEEP_DATA=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --keep-data) KEEP_DATA=1 ;;
        --yes) ASSUME_YES=1 ;;
        *) echo "Unknown option: $arg (use --keep-data and/or --yes)"; exit 1 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0"; exit 1; }

echo "This removes Lunapad:"
echo "  - the TV console autologin as $APP_USER (back to a normal login prompt)"
echo "  - /etc/sudoers.d/lunapad and /etc/apt/apt.conf.d/90lunapad-noprompt"
echo "  - $APP_DIR"
if [ "$KEEP_DATA" = 1 ]; then
    echo "  (keeping the $APP_USER account and its settings / Moonlight pairing: --keep-data)"
else
    echo "  - the $APP_USER account and /home/$APP_USER (settings, cache, Moonlight pairing)"
fi
if [ "$ASSUME_YES" != 1 ]; then
    read -r -p "Continue? [y/N] " answer
    case "$answer" in [yY]*) ;; *) echo "Nothing removed."; exit 0 ;; esac
fi

say "Console autologin"
if [ -f "$AUTOLOGIN" ] && grep -q -- "--autologin $APP_USER" "$AUTOLOGIN"; then
    rm -f "$AUTOLOGIN"
    rmdir --ignore-fail-on-non-empty "$(dirname "$AUTOLOGIN")"
    systemctl daemon-reload
    echo "Removed $APP_USER's autologin"
else
    echo "Autologin isn't Lunapad's - left alone"
fi

say "Stopping Lunapad"
systemctl restart getty@tty1 || true            # back to a login prompt, ending its session
loginctl terminate-user "$APP_USER" 2>/dev/null || true
pkill -u "$APP_USER" 2>/dev/null || true
sleep 2
pkill -9 -u "$APP_USER" 2>/dev/null || true

say "System files"
rm -f /etc/sudoers.d/lunapad /etc/apt/apt.conf.d/90lunapad-noprompt
echo "Removed /etc/sudoers.d/lunapad and /etc/apt/apt.conf.d/90lunapad-noprompt"

say "Program"
# This script may itself live in $APP_DIR: bash keeps reading it from the
# already-open file, so removing the folder now is safe.
rm -rf "$APP_DIR"
echo "Removed $APP_DIR"

if [ "$KEEP_DATA" = 1 ]; then
    say "Keeping the $APP_USER account and /home/$APP_USER"
elif id "$APP_USER" >/dev/null 2>&1; then
    say "Account"
    userdel --remove "$APP_USER" 2>/dev/null || userdel --remove --force "$APP_USER"
    echo "Removed the $APP_USER account and /home/$APP_USER"
fi

echo
echo "Lunapad is uninstalled. The TV shows a login prompt; to log in automatically"
echo "as yourself again: sudo raspi-config > System Options > Boot / Auto Login."
