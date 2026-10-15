# Lunapad — working notes

Gamepad-only Moonlight launcher on a Raspberry Pi 4 (pygame, `SDL_VIDEODRIVER=kmsdrm`, no desktop). See README.md for features and layout.

## Pi access

- Machine-specific details (host, accounts, keys) are in `CLAUDE.local.md`, which isn't committed. The deploy scripts take the Pi from `PI=user@host` or `deploy/pi-target`.
- On the Pi, Lunapad runs as the **`lunapad` account** (no password, console autologin on tty1, sudo only for reboot/apt/splash). Program in `/opt/lunapad` (a git checkout, root-owned); settings `/home/lunapad/.config/lunapad/lunapad.toml`; cache `/home/lunapad/.cache/lunapad/`; Moonlight's pairing in that account's `~/.config/Moonlight Game Streaming Project/`. SSH in as a separate admin account (in `CLAUDE.local.md`).

## Install and deploy

- **Install / repair / update to committed code:** `bash deploy/install-on-pi.sh` (sends the repo as a git bundle, runs `sudo ./install.sh` on the Pi). `install.sh` is idempotent: packages, the `lunapad` account and groups (video, render, input, audio, bluetooth, tty), `/opt/lunapad` checked out at `LUNAPAD_REF` / the highest `vX.Y.Z` tag / the source's HEAD, the system files copied from that installed version, `BUILD`, the login loop, sudoers (template filled in, `visudo`-checked), apt config, tty1 autologin override, then restarts `getty@tty1`.
- **Public install:** `curl -fsSL https://raw.githubusercontent.com/NSF12345/lunapad/main/install.sh | sudo bash`. Piped in, `install.sh` clones the repo into a temp folder and re-runs itself from there. Its body is inside `main()`, so a cut-off download never runs.
- **Dev deploy of the working copy:** `bash deploy/deploy.sh`. It uploads a tarball, then `deploy/remote-deploy.sh` runs as root on the Pi:
  1. `py_compile` everything before copying anything into `/opt/lunapad`; note the mtime
  2. `pkill -9 -u lunapad` the launcher. The `lunapad` account's tty1 loop relaunches it (can take more than 6s, so it polls for up to 30s).
  3. **Verify the new PID's start time is after the file mtime.** A process merely existing proves nothing: stale processes have been mistaken for fresh ones before.
  4. Re-check the PID a few seconds later to confirm it's stable.

  It ends with `OK:`, `WARN:` or `FAIL:`. BUILD reads `dev <describe> <date>`.
- **Uninstall:** `sudo /opt/lunapad/uninstall.sh [--keep-data] [--yes]` reverses `install.sh`: autologin (only if it's `--autologin lunapad`), sudoers and apt files, `/opt/lunapad`, and the account and home unless `--keep-data`. It leaves the apt packages and Moonlight. Keep it in step whenever `install.sh` gains something.
- Both upload first and then open a second SSH session for `sudo`: the payload is too big for an SSH command line (128 KB argument limit). So they ask for the password three times (upload, session, sudo) until an SSH key is set up.

## Gotchas learned the hard way

- **Never launch `lunapad.py` by hand while the tty1 loop runs.** Two instances fight over DRM master. (By hand would also mean as the wrong account.)
- A few rapid restarts right after `kill -9` are a transient kmsdrm lock race. They settle on their own.
- Exit code 99 (the on-screen Exit) breaks the tty1 loop. To recover: `sudo systemctl restart getty@tty1` (autologin starts the loop again), or reboot.
- `SDL_VIDEODRIVER=dummy` ignores the requested resolution, which breaks layout math (e.g. "Invalid resolution for Surface"). Don't use it to judge layout.
- Screenshots: `/dev/fb0` shows the console, not the app. There's no capture hook now (the old one-shot hook on the Bluetooth page is in the private repo's history if ever needed).
- Non-ASCII text sent through `plink` gets corrupted to `?`. Use `chr(0x16BC)`-style escapes.
- SDL controller names differ from bluez/sysfs names ("Google Stadia Controller" vs "StadiaZVNQ-6ccb"). Don't match them as exact strings.
- Menu input goes through SDL's GameController API, which sends each press twice (`CONTROLLER*` and a raw `JOY*` twin). `handle_device_event()` drops the twin, so route every event loop through it and `input_action()`.
- Bluetooth gamepads report `Icon: input-gaming` in `bluetoothctl info`, which is how the scan list is filtered. Active scanning has dropped an existing pairing before.
- The app tiles come from `moonlight-qt list` and need **Moonlight 6.2.0+**. Before that, `list` printed a stale cached list that only updated when the Moonlight GUI ran. Lunapad worked around it by calling the host's `/applist` directly with Moonlight's pairing cert. That workaround was removed once 6.2.0 shipped the fix; it's in git history if ever needed.
- `moonlight-qt list --csv` (6.2.0) first downloads any box art Moonlight hasn't cached, and in CLI mode that **hangs** (`QEventLoop: Cannot be used without QApplication`, `appasset` requests in the log). Seen as the new `lunapad` account with an empty cache. So app *names* come from plain `list` (fast, reliable), and *IDs* from `--csv` in a background thread with its own limit, keeping known IDs if it stalls. Opening Moonlight Window once caches the box art. Worth reporting upstream.
- Running-session detection: Sunshine's `serverinfo` (plain HTTP, port 47989, no pairing) reports `currentgame`, the running app's ID. It's matched to tiles via the IDs from `moonlight-qt list --csv`. Streaming the running app resumes it, and `moonlight-qt quit HOST` ends it. Verified on the Pi: resume, B to quit, and Close & Launch.
- Run `moonlight-qt list` (or any non-stream Moonlight command) with `QT_QPA_PLATFORM=offscreen` while the launcher is up. Otherwise Qt grabs the DRM display the launcher holds (`Failed to move cursor ... -13`). Offscreen output is just the app names, one per line.
- Dashboard navigation is spatial (`nav_move()` over `tile_rects`), not a map. Sideways moves stay in the row, and reversing a move goes back where you came from.
- Moonlight rewrites its own `Moonlight.conf` (which also holds a private key, so never commit it). Use CLI flags such as `--performance-overlay` instead of editing it.
- Settings: `settings.py`'s `SCHEMA` is the single source for `~/.config/lunapad/lunapad.toml` and the Settings page. Add a setting there (`page=False` for file-only, `restart=True` if it's read only at startup) and read it with `cfg.get("section.key")` where it's used, so page changes apply live. Every stream option is passed explicitly (`cfg.stream_args()`), so Moonlight's own saved preferences never decide a stream.
- Host port: Moonlight's CLI takes `host:port` (parsed as a URL; the default port when none is given is 47989). `cfg.moonlight_host()` gives the bare host on the default port, so the default setup sends exactly what it always did. The serverinfo check uses `HOST_PORT` too.
- Moonlight is a dependency Lunapad only *checks*: `moonlight_present` (`shutil.which`, re-checked with the 5s status refresh) and `moonlight_version` (`moonlight-qt --version`). `require_moonlight()` guards everything that runs it. Never auto-install it; the official install runs a downloaded script as root.
- Pairing (Moonlight 6.2.0, tested on the Pi): `list` on an unpaired host prints "...has not been paired..." and exits 255. `pair HOST --pin NNNN` waits for the PIN to be entered in Sunshine, then exits 0. But `pair` on an already-paired host **hangs forever** (a hidden dialog). Run from the launcher, `pair` also didn't exit after a correct PIN. So `run_pairing()` checks with `list` first, never waits on `pair`'s exit (a background thread polls `list` every 3s and finishes as soon as it works), caps it at PAIR_TIMEOUT, and lets B cancel it (with a final `list` check). Background Moonlight commands use `moonlight_env()`: offscreen, without the launcher's `SDL_VIDEODRIVER=kmsdrm`.
- No default host: a fresh install has `host.address = ""`, and the dashboard shows a **Set up host** tile (keyboard entry, then a restart) instead of the apps. Host-dependent checks skip themselves while it's empty. `LUNAPAD_HOST` presets it at install time (only into a new settings file).
- Update's unused-package offer (`offer_autoremove()`, via `apt-get -s autoremove`) runs only after a successful update run, never on the Update button press (the simulation stalled the menu). To test it: `sudo apt-get install -y sl && sudo apt-mark auto sl`, then Update Now (it runs even with nothing to upgrade).
- Self-update: the Update screen runs `sudo /bin/bash /opt/lunapad/deploy/self-update.sh stable|beta` (sudo rule: exactly those two). It fetches from the public repo, checks out the target, then `exec`s the new `install.sh --update`, which skips the account, autologin and restart steps. The launcher then exits 0 and the tty1 loop starts the new code. `self-update.sh`'s body sits inside `main()` because the checkout rewrites the file while bash is still reading it. The check (`channel_target()`) is a `git ls-remote` with no root needed, compared against `git rev-parse HEAD` in `/opt/lunapad`. A dev deploy or `install-on-pi.sh` checkout is just a different HEAD, so the next update replaces it.
- Popups take a `draw_bg` callback so they render over the calling screen, not the dashboard.

## Working preferences

- Show visual/layout changes (render or screenshot) before deploying them. `python tools/preview_dashboard.py` renders the dashboard at 1920x1080 for 0/1/2/4 controllers (needs `pip install pygame-ce` locally).
- After changing how a screen looks, re-run `python tools/readme_screenshots.py` so the README images stay current. They use sample data only.
- On ambiguous visuals (icons, shapes), ask instead of guessing repeatedly.
- Check what SDL recognises with `deploy/check_controllers.py`, never from assumptions.

## Todo

Nothing outstanding. v0.7.0, the first tagged release, has been tested on the Pi through both channels. There's deliberately no v0.6.0 tag, so every tagged release can update itself.

## Git

- Public repo `NSF12345/lunapad`. **Never push to `main`**: work on feature branches, and the user merges them (into `beta`, then `main`) when they choose. The `beta` branch is what beta-channel Pis install.
- No local merge commits: bring a feature branch up to date by rebasing it onto `beta`. `main` and `beta` are protected (pull request required, no deletion, no force push), so nothing is pushed to them directly. `beta` may lag `main` by its PR merge commits; that's harmless.
- Merge `beta` -> `main` with a merge commit, never a squash: a squashed long-lived branch keeps showing its old commits as unmerged in every later PR.
- PR checks (`.github/workflows/`): `checks.yml` runs ruff + `py_compile`, shellcheck + a `visudo` check of the sudoers template, `.github/scripts/check-private-details.sh` (private LAN IPs other than the 192.168.1.x examples, non-noreply emails) and gitleaks; `codeql.yml` runs CodeQL. Run them locally before pushing: `ruff check .`, `shellcheck install.sh uninstall.sh deploy/*.sh deploy/bash_profile .github/scripts/*.sh`, `bash .github/scripts/check-private-details.sh`. Lint rules are pinned in `ruff.toml` (real problems only, not style).
- Commits use the GitHub no-reply email. Keep private details in the git-ignored `CLAUDE.local.md` and `deploy/pi-target`.
