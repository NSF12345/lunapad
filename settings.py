"""Lunapad settings: one schema that drives both the config file
(~/lunapad.toml) and the gamepad settings page.

Each setting has a dotted key ("stream.fps"), a label, a kind, allowed values,
a default (today's behaviour) and help text. The file is created with defaults
on first run, can be edited by hand, and is rewritten - with every setting
documented - whenever the settings page changes something.
"""
import json, os, re, tomllib

# Where things live, for whichever user runs Lunapad (XDG-style). The
# LUNAPAD_*_DIR variables override them, e.g. for the off-screen preview tool.
HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("LUNAPAD_CONFIG_DIR") or os.path.join(HOME, ".config", "lunapad")
CACHE_DIR = os.environ.get("LUNAPAD_CACHE_DIR") or os.path.join(HOME, ".cache", "lunapad")
CONFIG_FILE = os.path.join(CONFIG_DIR, "lunapad.toml")
# Internal state files - not settings.
STATE_FILE = os.path.join(CACHE_DIR, "update_state.json")
APPS_CACHE_FILE = os.path.join(CACHE_DIR, "apps.json")


def ensure_dirs():
    """Creates the config and cache folders if they don't exist yet."""
    for d in (CONFIG_DIR, CACHE_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"Couldn't create {d}: {e}")


class Setting:
    def __init__(self, key, label, kind, default, help, choices=None, page=True, restart=False):
        self.key, self.label, self.kind, self.default, self.help = key, label, kind, default, help
        self.choices = choices  # [(value, label), ...] for kind "choice"
        self.page = page        # shown on the gamepad settings page
        self.restart = restart  # only takes effect after the launcher restarts

    @property
    def section(self):
        return self.key.split(".")[0]


def _minutes(values, never_label="Never"):
    return [(v, never_label if v == 0 else f"{v} min") for v in values]


SCHEMA = [
    # --- host ---
    Setting("host.address", "Host address", "host", "",
            "Hostname or IP address of the gaming PC running Sunshine. Empty until set up.", restart=True),
    Setting("host.port", "Host port", "port", 47989,
            "Sunshine's HTTP port - 47989 unless you changed Sunshine's port settings.", restart=True),

    # --- stream ---
    Setting("stream.resolution", "Resolution", "choice", "1080",
            "Resolution the PC streams at.",
            choices=[("720", "720p"), ("1080", "1080p"), ("1440", "1440p"), ("4K", "4K")]),
    Setting("stream.fps", "Frame rate", "choice", 30,
            "Frames per second the PC streams at.",
            choices=[(30, "30 fps"), (60, "60 fps"), (90, "90 fps"), (120, "120 fps")]),
    Setting("stream.bitrate_mbps", "Bitrate", "choice", 0,
            "Video bitrate. Auto lets Moonlight pick one for the resolution and frame rate.",
            choices=[(0, "Auto")] + [(v, f"{v} Mbps") for v in (5, 10, 15, 20, 30, 40, 50, 80, 100)]),
    Setting("stream.codec", "Video codec", "choice", "auto",
            "Video codec. Auto picks the best one both sides support.",
            choices=[("auto", "Auto"), ("H.264", "H.264"), ("HEVC", "HEVC"), ("AV1", "AV1")]),
    Setting("stream.performance_overlay", "Performance overlay", "bool", True,
            "Show Moonlight's stats overlay (resolution, fps, latency) during streams."),
    Setting("stream.audio", "Audio", "choice", "stereo",
            "Audio channels to stream.",
            choices=[("stereo", "Stereo"), ("5.1-surround", "5.1 surround"), ("7.1-surround", "7.1 surround")]),
    Setting("stream.vsync", "V-Sync", "bool", True,
            "Sync frames to the display - prevents tearing, can add a little latency."),
    Setting("stream.frame_pacing", "Frame pacing", "bool", False,
            "Smooth out uneven frame timing, at the cost of a little latency."),
    Setting("stream.swap_ab", "Swap A/B and X/Y", "bool", False,
            "Swap the face buttons in streams (for Nintendo-style layouts)."),
    Setting("stream.quit_app_after", "Quit app when stream ends", "bool", False,
            "Close the app on the PC when you leave the stream, instead of leaving it to resume."),

    # --- display ---
    Setting("display.resolution", "Screen resolution", "choice", "1920x1080",
            "The launcher's own resolution on the TV.", restart=True,
            choices=[("native", "Native"), ("1280x720", "720p"), ("1920x1080", "1080p"), ("3840x2160", "4K")]),

    # --- idle ---
    Setting("idle.dim_after_minutes", "Dim screen after", "choice", 5,
            "Minutes without input before the screen dims (burn-in protection).",
            choices=_minutes((1, 2, 5, 10, 15, 30, 0))),
    Setting("idle.black_after_minutes", "Then black after", "choice", 5,
            "Minutes the screen stays dimmed before fading to black.",
            choices=_minutes((1, 2, 5, 10, 15, 30, 0), never_label="Stay dimmed")),
    Setting("idle.disconnect_after_minutes", "Disconnect controllers after", "choice", 10,
            "Minutes without input before Bluetooth gamepads are disconnected to save battery.",
            choices=_minutes((5, 10, 20, 30, 60, 0))),
    Setting("idle.dim_level", "Dim level", "int", 150,
            "How dark the dimmed stage is: 0 = untouched, 255 = black.", page=False),
    Setting("idle.fade_seconds", "Fade time", "int", 15,
            "Seconds each fade (to dimmed, then to black) takes.", page=False),

    # --- file-only ---
    Setting("apps.refresh_minutes", "App list refresh", "int", 10,
            "How often to re-read the host's app list (Reload Apps does it immediately).", page=False),
    Setting("apps.fallback", "Fallback apps", "str_list", ["Desktop", "Steam Big Picture"],
            "App tiles to show until the host's list has been read once.", page=False),
    Setting("updates.check_hours", "Update check", "int", 24,
            "How often to check for Pi system updates.", page=False),
    Setting("input.stick_trigger", "Stick trigger", "float", 0.5,
            "How far (0-1) a stick must be pushed to move the selection.", page=False),
    Setting("input.stick_release", "Stick release", "float", 0.3,
            "How close to centre (0-1) a stick must return before it can move again.", page=False),
    Setting("controllers.wireless_dongles", "Wireless receivers", "table", {"2dc8:310a": "2.4G"},
            "USB receivers for wireless pads: \"vendor:product\" (lowercase hex, see `lsusb`) "
            "-> the label shown instead of USB.", page=False),
]
BY_KEY = {s.key: s for s in SCHEMA}

SECTION_TITLES = {
    "host": "Host", "stream": "Moonlight", "display": "Launcher display", "idle": "Idle",
    "apps": "Apps", "updates": "Updates", "input": "Input", "controllers": "Controllers",
}
# Shown next to a section's title on the Settings page, and above it in the file.
SECTION_NOTES = {
    "stream": "Applied when a stream starts, and override Moonlight's own settings",
}

_values = {}
_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$", re.I)
DEFAULT_HTTP_PORT = 47989  # Moonlight's default when no port is given


def _valid(s, v):
    if s.kind == "choice":
        return v in [c for c, _ in s.choices]
    if s.kind == "bool":
        return isinstance(v, bool)
    if s.kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if s.kind == "float":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if s.kind == "host":  # hostname or IPv4 (labels of letters/digits/hyphens), or "" = not set up yet
        return isinstance(v, str) and (v == "" or bool(_HOSTNAME.match(v)))
    if s.kind == "port":
        return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535
    if s.kind == "str_list":
        return isinstance(v, list) and v and all(isinstance(x, str) for x in v)
    if s.kind == "table":
        return isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items())
    return False


def load():
    """Reads CONFIG_FILE into memory, falling back to the default for any
    missing or invalid value (and saying so). Creates the file if missing."""
    ensure_dirs()
    data, exists = {}, os.path.exists(CONFIG_FILE)
    if exists:
        try:
            with open(CONFIG_FILE, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            print(f"Settings: couldn't read {CONFIG_FILE} ({e}) - using defaults")
    for s in SCHEMA:
        section, name = s.key.split(".", 1)
        v = data.get(section, {}).get(name, s.default)
        if isinstance(v, int) and not isinstance(v, bool) and s.kind == "float":
            v = float(v)
        if not _valid(s, v):
            print(f"Settings: invalid {s.key} = {v!r} - using default {s.default!r}")
            v = s.default
        _values[s.key] = v
    if not exists:
        save()


def get(key):
    return _values.get(key, BY_KEY[key].default)


def set(key, value):
    """Changes a setting and saves the file. Returns False if the value isn't allowed."""
    if not _valid(BY_KEY[key], value):
        return False
    _values[key] = value
    save()
    return True


def value_label(key, value=None):
    s = BY_KEY[key]
    v = get(key) if value is None else value
    if s.kind == "choice":
        return dict(s.choices).get(v, str(v))
    if s.kind == "bool":
        return "On" if v else "Off"
    if s.kind == "host" and not v:
        return "Not set"
    return str(v)


def cycle(key, step):
    """Next (+1) or previous (-1) value for a choice/bool setting, wrapping."""
    s = BY_KEY[key]
    if s.kind == "bool":
        return not get(key)
    options = [c for c, _ in s.choices]
    i = options.index(get(key)) if get(key) in options else 0
    return options[(i + step) % len(options)]


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    raise TypeError(v)


def save():
    lines = ["# Lunapad settings. Edit by hand or from the Settings page on the Pi.",
             "# The page rewrites this file: values are kept, other comments aren't.",
             "# Settings marked (restart) take effect when the launcher restarts.", ""]
    tables = []
    for section in dict.fromkeys(s.section for s in SCHEMA):
        if section in SECTION_NOTES:
            lines.append(f"# {SECTION_NOTES[section]}.")
        lines.append(f"[{section}]")
        for s in [s for s in SCHEMA if s.section == section]:
            name, v = s.key.split(".", 1)[1], get(s.key)
            note = s.help + (" (restart)" if s.restart else "")
            if s.kind == "choice":
                note += "  Options: " + ", ".join(
                    _toml(c) if str(c) == label or _toml(c) == f'"{label}"' else f"{_toml(c)} ({label})"
                    for c, label in s.choices)
            if s.kind == "table":
                tables.append((s, v))
                continue
            lines += [f"# {note}", f"{name} = {_toml(v)}", ""]
    for s, table in tables:
        lines += [f"# {s.help}", f"[{s.key}]"] + [f"{_toml(k)} = {_toml(x)}" for k, x in table.items()] + [""]
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        print(f"Settings: couldn't save {CONFIG_FILE}: {e}")


def stream_args():
    """Moonlight `stream` options from the settings. Every option is passed
    explicitly, so Moonlight's own saved preferences never decide a stream."""
    def toggle(flag, key):
        return f"--{flag}" if get(key) else f"--no-{flag}"
    args = [f"--{get('stream.resolution')}", "--fps", str(get("stream.fps"))]
    if get("stream.bitrate_mbps"):
        args += ["--bitrate", str(get("stream.bitrate_mbps") * 1000)]  # Moonlight takes Kbps
    args += ["--video-codec", get("stream.codec"), "--audio-config", get("stream.audio"),
             toggle("performance-overlay", "stream.performance_overlay"),
             toggle("vsync", "stream.vsync"), toggle("frame-pacing", "stream.frame_pacing"),
             toggle("swap-gamepad-buttons", "stream.swap_ab"), toggle("quit-after", "stream.quit_app_after")]
    return args


def moonlight_host():
    """The host as Moonlight's CLI takes it: just the address on the default
    port (exactly as before ports were configurable), else "address:port"
    (Moonlight parses it as a URL, so IPv6 needs brackets)."""
    host, port = get("host.address"), get("host.port")
    if port == DEFAULT_HTTP_PORT:
        return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def display_resolution():
    """(width, height) for the launcher's screen, or None for native."""
    v = get("display.resolution")
    return None if v == "native" else tuple(int(x) for x in v.split("x"))
