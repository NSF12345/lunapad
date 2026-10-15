import os, sys, re, csv, random, shutil, subprocess, threading, time, json, socket, ipaddress, glob, pygame
import urllib.request
import xml.etree.ElementTree as ET
import settings as cfg
from settings import STATE_FILE, APPS_CACHE_FILE

__version__ = "0.6.0"
APP_DIR = os.path.dirname(os.path.abspath(__file__))  # /opt/lunapad on the Pi: code and images

def _read_build():
    """install.sh / deploy.sh write the version (git describe) and date into BUILD."""
    try:
        with open(os.path.join(APP_DIR, "BUILD")) as f:
            return f.read().strip()
    except OSError:
        return ""

BUILD = _read_build()
VERSION_TEXT = f"Lunapad {__version__}" + (f" ({BUILD})" if BUILD else "")
# the dashboard's short form, next to the title: the build without its date
_build_short = re.sub(r"\s+\d{4}-\d{2}-\d{2}$", "", BUILD)
VERSION_SHORT = f"v{__version__}" + (f" ({_build_short})" if _build_short else "")

# Settings live in ~/.config/lunapad/lunapad.toml (see settings.py); these few are read once
# at startup - changing them on the Settings page asks for a launcher restart.
cfg.load()

# --- Log file: everything printed goes to ~/.cache/lunapad/lunapad.log -------
# (the TV console isn't readable, and Lunapad runs as its own account)
LOG_FILE = os.path.join(cfg.CACHE_DIR, "lunapad.log")
LOG_MAX_BYTES = 1_000_000

def start_log():
    try:
        mode = "w" if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES else "a"
        log = open(LOG_FILE, mode, buffering=1, encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"Couldn't open {LOG_FILE}: {e}")
        return
    sys.stdout = sys.stderr = log
    print()
    print(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {VERSION_TEXT}  pid {os.getpid()} "
          f"user {os.environ.get('USER', '?')} =====")

if sys.platform.startswith("linux"):  # on the Pi; the PC preview keeps printing to the terminal
    start_log()
HOST = cfg.get("host.address")
HOST_PORT = cfg.get("host.port")  # Sunshine's HTTP port
MOONLIGHT_HOST = cfg.moonlight_host()  # what moonlight-qt is given: host, or host:port
HOST_URL_HOST = f"[{HOST}]" if ":" in HOST else HOST  # for http:// URLs
RESOLUTION = cfg.display_resolution()
STICK_TRIGGER = cfg.get("input.stick_trigger")
STICK_RELEASE = cfg.get("input.stick_release")
CHECK_INTERVAL = cfg.get("updates.check_hours") * 3600
RESTART_SETTINGS_AT_START = {st.key: cfg.get(st.key) for st in cfg.SCHEMA if st.restart}

# Fixed tiles. The D-pad moves to the nearest tile in the pressed direction,
# so there's no navigation map to maintain.
NODES = {
    "moonlight": {"label": "Moonlight Window", "cmd": ["moonlight-qt"]},
    "bluetooth": {"label": "Bluetooth", "cmd": "BLUETOOTH"},
    "update": {"label": "Update", "cmd": "UPDATE"},
    "reload": {"label": "Reload Apps", "cmd": "RELOAD_APPS"},
    "reboot": {"label": "Reboot", "cmd": "REBOOT"},
    "exit": {"label": "Exit", "cmd": None},
    "settings": {"label": "Settings", "cmd": "SETTINGS"},  # chip in the host panel
    "pair": {"label": "Pair with Sunshine", "cmd": "PAIR"},  # replaces the app tiles while unpaired
    "sethost": {"label": "Set up host", "cmd": "SET_HOST"},  # replaces them while no host is set
}

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")

def axis_pair_direction(event, state, x_axis=0, y_axis=1):
    """Edge-triggered direction from a JOYAXISMOTION event on a given (x, y)
    axis pair. `state` is a dict the caller owns (fresh per stick/context)
    tracking which axis is currently held past the trigger threshold, so one
    push yields one direction."""
    if event.axis == x_axis:
        if event.value < -STICK_TRIGGER and not state.get("left"):
            state["left"], state["right"] = True, False
            return "left"
        elif event.value > STICK_TRIGGER and not state.get("right"):
            state["right"], state["left"] = True, False
            return "right"
        elif abs(event.value) < STICK_RELEASE and (state.get("left") or state.get("right")):
            state["left"] = state["right"] = False
            return "center"
    elif event.axis == y_axis:
        if event.value < -STICK_TRIGGER and not state.get("up"):
            state["up"], state["down"] = True, False
            return "up"
        elif event.value > STICK_TRIGGER and not state.get("down"):
            state["down"], state["up"] = True, False
            return "down"
        elif abs(event.value) < STICK_RELEASE and (state.get("up") or state.get("down")):
            state["up"] = state["down"] = False
            return "center"
    return None

def stick_direction(event, state):
    return axis_pair_direction(event, state, 0, 1)

def get_client_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect((HOST or "1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"

CLIENT_IP = get_client_ip()

def format_host_display(host):
    if not host:
        return "No host"
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        try:
            resolved = socket.gethostbyname(host)
            return f"{host} ({resolved})"
        except Exception:
            return f"{host} (unresolved)"

HOST_DISPLAY = format_host_display(HOST) + (f" port {HOST_PORT}" if HOST_PORT != cfg.DEFAULT_HTTP_PORT else "")

# --- Input: SDL GameController API ---
# Pads in SDL's controller database (the same one Moonlight uses) arrive as
# CONTROLLER* events in a standard Xbox-style layout, so no per-controller
# button maps are needed. SDL also sends raw JOY* twins of those events,
# which are dropped. Pads SDL doesn't know fall back to raw JOY* events with
# a basic guess (A=0, B=1, Y=3, hat D-pad, left stick) - fix those
# properly with an SDL_GAMECONTROLLERCONFIG line, which fixes Moonlight too.
try:
    from pygame._sdl2 import controller as sdl_controller
except ImportError:
    sdl_controller = None
    print("pygame._sdl2.controller unavailable - using raw joystick input")

joysticks = {}    # instance_id -> pygame.joystick.Joystick, every device
controllers = {}  # instance_id -> sdl Controller, for SDL-recognised pads only

def add_input_device(device_index):
    j = pygame.joystick.Joystick(device_index)
    j.init()
    iid = j.get_instance_id()
    joysticks[iid] = j
    if sdl_controller and sdl_controller.is_controller(device_index):
        controllers[iid] = sdl_controller.Controller(device_index)
    kind = "controller" if iid in controllers else "raw joystick"
    print(f"Input device added: {j.get_name()} ({kind})")

def remove_input_device(instance_id):
    joysticks.pop(instance_id, None)
    controllers.pop(instance_id, None)

def init_input():
    joysticks.clear()
    controllers.clear()
    pygame.joystick.init()
    if sdl_controller:
        sdl_controller.init()
    for i in range(pygame.joystick.get_count()):
        add_input_device(i)

RAW_INPUT_TYPES = (pygame.JOYAXISMOTION, pygame.JOYHATMOTION, pygame.JOYBUTTONDOWN)
INPUT_EVENT_TYPES = RAW_INPUT_TYPES + (
    pygame.CONTROLLERAXISMOTION, pygame.CONTROLLERBUTTONDOWN, pygame.KEYDOWN)

_CONTROLLER_BUTTON_ACTIONS = {
    pygame.CONTROLLER_BUTTON_A: "a",
    pygame.CONTROLLER_BUTTON_B: "b",
    pygame.CONTROLLER_BUTTON_X: "x",
    pygame.CONTROLLER_BUTTON_Y: "y",
    pygame.CONTROLLER_BUTTON_START: "start",
    pygame.CONTROLLER_BUTTON_RIGHTSHOULDER: "rb",
    pygame.CONTROLLER_BUTTON_GUIDE: "home",
    pygame.CONTROLLER_BUTTON_DPAD_UP: "up",
    pygame.CONTROLLER_BUTTON_DPAD_DOWN: "down",
    pygame.CONTROLLER_BUTTON_DPAD_LEFT: "left",
    pygame.CONTROLLER_BUTTON_DPAD_RIGHT: "right",
}
_RAW_BUTTON_ACTIONS = {0: "a", 1: "b", 2: "x", 3: "y", 5: "rb"}  # raw D-pads are usually a hat
_KEY_ACTIONS = {
    pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left", pygame.K_RIGHT: "right",
    pygame.K_RETURN: "a", pygame.K_ESCAPE: "b", pygame.K_y: "y", pygame.K_HOME: "home",
}

class _AxisEvent:
    """Adapter so axis_pair_direction can read controller axes, which SDL
    reports as -32768..32767 rather than the joystick API's -1..1."""
    def __init__(self, axis, value):
        self.axis, self.value = axis, value

last_input_at = {}  # SDL instance id -> time of its last deliberate input (card indicator)
INPUT_FLASH_SECONDS = 0.5  # card outline flash: full brightness fading to nothing

def _is_deliberate_input(event):
    """A button, D-pad or a stick pushed past halfway - not stick drift."""
    if event.type in (pygame.CONTROLLERBUTTONDOWN, pygame.JOYBUTTONDOWN):
        return True
    if event.type == pygame.JOYHATMOTION:
        return event.value != (0, 0)
    if event.type == pygame.CONTROLLERAXISMOTION:
        return abs(event.value) > 16384
    if event.type == pygame.JOYAXISMOTION:
        return abs(event.value) > 0.5
    return False

def handle_device_event(event):
    """Shared by every screen's event loop. Tracks pads being added/removed,
    and returns True for events that must be ignored: the raw JOY* twin of an
    event SDL also delivers as CONTROLLER*, and any input while the screen
    is dimmed (it wakes the screen instead of acting)."""
    if event.type == pygame.JOYDEVICEADDED:
        add_input_device(event.device_index)
    elif event.type == pygame.JOYDEVICEREMOVED:
        remove_input_device(event.instance_id)
    if event.type not in INPUT_EVENT_TYPES:
        return False
    if event.type in RAW_INPUT_TYPES and event.instance_id in controllers:
        return True
    if _is_deliberate_input(event):
        last_input_at[event.instance_id] = time.time()
    if is_screen_dimmed():
        note_activity()
        return True
    note_activity()
    return False

def input_action(event, stick_state):
    """Translates one input event into "up"/"down"/"left"/"right"/"a"/"b"/"y"/
    "home", or None. `stick_state` is the caller's edge-trigger state, so a
    held stick moves the selection once per push."""
    if event.type == pygame.CONTROLLERBUTTONDOWN:
        return _CONTROLLER_BUTTON_ACTIONS.get(event.button)
    if event.type == pygame.CONTROLLERAXISMOTION:
        if event.axis in (pygame.CONTROLLER_AXIS_LEFTX, pygame.CONTROLLER_AXIS_LEFTY):
            # SDL gives whole numbers -32768..32767; scale every int (a raw -1
            # near centre is NOT "fully up" - that bug made sticks bounce).
            value = event.value / 32767 if isinstance(event.value, int) else event.value
            direction = stick_direction(_AxisEvent(event.axis, value), stick_state)
            return None if direction == "center" else direction
        return None
    if event.type == pygame.JOYBUTTONDOWN:
        return _RAW_BUTTON_ACTIONS.get(event.button)
    if event.type == pygame.JOYHATMOTION:
        hx, hy = event.value
        return "up" if hy == 1 else "down" if hy == -1 else "left" if hx == -1 else "right" if hx == 1 else None
    if event.type == pygame.JOYAXISMOTION:
        direction = stick_direction(event, stick_state)
        return None if direction == "center" else direction
    if event.type == pygame.KEYDOWN:
        return _KEY_ACTIONS.get(event.key)
    return None

pygame.init()
init_input()

screen = pygame.display.set_mode(RESOLUTION or (0, 0), pygame.FULLSCREEN)
pygame.mouse.set_visible(False)
w, h = screen.get_size()

title_font = pygame.font.SysFont(None, 96)
subtitle_font = pygame.font.SysFont(None, 40)
item_font = pygame.font.SysFont(None, 56)
hint_font = pygame.font.SysFont(None, 32)
badge_font = pygame.font.SysFont(None, 32)
caption_font = pygame.font.SysFont(None, 18)

bg = None
if os.path.exists(os.path.join(APP_DIR, "splash.png")):
    bg = pygame.image.load(os.path.join(APP_DIR, "splash.png"))
    bg = pygame.transform.scale(bg, (w, h))


THEMES = {
    "dark": {
        "bg": (14, 14, 20),
        "panel_bg": (18, 18, 26),
        "tile_bg": (30, 30, 42),
        "accent": (90, 170, 255),
        "text": (235, 235, 240),
        "muted": (150, 150, 160),
        "warn": (240, 170, 60),
        "good": (110, 200, 110),
        "bad": (200, 90, 90),
        "divider": (60, 60, 75),
    },
}
CURRENT_THEME = "dark"
THEME = THEMES[CURRENT_THEME]

ACCENT = THEME["accent"]
TEXT = THEME["text"]
MUTED = THEME["muted"]
PANEL_BG = THEME["panel_bg"]
WARN = THEME["warn"]
TILE_BG = THEME["tile_bg"]
GOOD = THEME["good"]
BAD = THEME["bad"]
DIVIDER = THEME["divider"]

_BLUETOOTH_ICON_PATH = os.path.join(APP_DIR, "bluetooth_icon.png")
_BLUETOOTH_ICON_RAW = None
_BLUETOOTH_ICON_CACHE = {}
if os.path.exists(_BLUETOOTH_ICON_PATH):
    _BLUETOOTH_ICON_RAW = pygame.image.load(_BLUETOOTH_ICON_PATH).convert_alpha()

def draw_bluetooth_icon(surface, center, size, color):
    cx, cy = center
    if _BLUETOOTH_ICON_RAW is None:
        pygame.draw.circle(surface, (0, 130, 252), (cx, cy), size / 2)
        return
    if size not in _BLUETOOTH_ICON_CACHE:
        _BLUETOOTH_ICON_CACHE[size] = pygame.transform.smoothscale(_BLUETOOTH_ICON_RAW, (size, size))
    icon = _BLUETOOTH_ICON_CACHE[size]
    surface.blit(icon, icon.get_rect(center=(cx, cy)))

def draw_battery_icon(surface, x, y, pct, charging=False):
    """Draws a fill-level battery icon (color reflects charge) at (x, y);
    returns the total width consumed. `charging` is supported for when a
    data source actually reports it - nothing currently does."""
    w, h = 28, 14
    body = pygame.Rect(x, y, w, h)
    pygame.draw.rect(surface, MUTED, body, width=2, border_radius=3)
    nub = pygame.Rect(x + w, y + h // 2 - 3, 3, 6)
    pygame.draw.rect(surface, MUTED, nub, border_radius=1)

    if pct <= 20:
        fill_color = (220, 90, 90)
    elif pct <= 50:
        fill_color = (230, 180, 60)
    else:
        fill_color = (110, 200, 110)

    fill_w = max(2, int((w - 4) * min(pct, 100) / 100))
    fill_rect = pygame.Rect(x + 2, y + 2, fill_w, h - 4)
    pygame.draw.rect(surface, fill_color, fill_rect, border_radius=2)

    if charging:
        bolt = [
            (x + w * 0.52, y - 2), (x + w * 0.30, y + h * 0.58), (x + w * 0.48, y + h * 0.58),
            (x + w * 0.38, y + h + 2), (x + w * 0.72, y + h * 0.38), (x + w * 0.52, y + h * 0.38),
        ]
        pygame.draw.polygon(surface, (20, 20, 15), bolt)

    return w + 3 + 8  # body + nub + trailing gap

def draw_battery_unknown_icon(surface, x, y):
    """Battery outline with no fill level, for when no battery data is
    available - returns the total width consumed. Pair with a '?' label
    blit where the percentage text would normally go."""
    w, h = 28, 14
    body = pygame.Rect(x, y, w, h)
    pygame.draw.rect(surface, MUTED, body, width=2, border_radius=3)
    nub = pygame.Rect(x + w, y + h // 2 - 3, 3, 6)
    pygame.draw.rect(surface, MUTED, nub, border_radius=1)
    return w + 3 + 8

selected = None  # tile key; None = the first app (set once tiles are laid out)
tile_rects = {}  # tile key -> Rect, rebuilt by every draw_frame()

def app_key(app):
    return "app:" + app

def node_label(key):
    return key[4:] if key.startswith("app:") else NODES[key]["label"]

def node_cmd(key):
    if key.startswith("app:"):
        return ["moonlight-qt", "stream", MOONLIGHT_HOST, key[4:], *cfg.stream_args()]
    return NODES[key]["cmd"]

_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
_last_nav = None  # (from_key, to_key, direction) of the previous move

def nav_move(current, direction):
    """The nearest tile in `direction` from `current`, or `current` if none.
    Left/right only move within the same row. Up/down prefer tiles straight
    above/below, then the nearest edge, then the nearest centre - so the grid
    needs no hand-written navigation map. Reversing a move goes back where
    you came from (e.g. Reboot -> up -> down lands on Reboot, not Exit)."""
    global _last_nav
    if (_last_nav and _last_nav[1] == current and _OPPOSITE[_last_nav[2]] == direction
            and _last_nav[0] in tile_rects):
        target = _last_nav[0]
        _last_nav = (current, target, direction)
        return target
    target = _nearest_tile(current, direction)
    if target != current:
        _last_nav = (current, target, direction)
    return target

def _nearest_tile(current, direction):
    cur = tile_rects.get(current)
    if cur is None:
        return current
    best, best_score = current, None
    for key, r in tile_rects.items():
        if key == current:
            continue
        if direction in ("left", "right"):
            gap = r.left - cur.right if direction == "right" else cur.left - r.right
            lo, hi, clo, chi = r.top, r.bottom, cur.top, cur.bottom
            offset = abs(r.centery - cur.centery)
        else:
            gap = r.top - cur.bottom if direction == "down" else cur.top - r.bottom
            lo, hi, clo, chi = r.left, r.right, cur.left, cur.right
            offset = abs(r.centerx - cur.centerx)
        if gap < -5:  # not in that direction
            continue
        overlaps = min(hi, chi) - max(lo, clo) > 0
        if direction in ("left", "right") and not overlaps:
            continue  # sideways stays in the row - never jump between panels
        score = (not overlaps, gap, offset)
        if best_score is None or score < best_score:
            best, best_score = key, score
    return best
status_message = None
clock = pygame.time.Clock()
running = True

# --- Controller status (bluetoothctl, refreshed periodically) ---
# Linux-side details per joystick device (connection, battery, MAC), refreshed
# in the background; build_controller_cards() joins them to SDL's controllers.
linux_controllers = []

def get_device_info(mac):
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=3
        )
        out = result.stdout
        name = mac
        battery = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                name = line.split("Name:", 1)[1].strip()
            elif line.startswith("Battery Percentage:") and "(" in line:
                battery = line.split("(")[1].split(")")[0]
        return name, battery
    except Exception:
        return mac, None

def list_js_devices():
    devices = []
    for path in sorted(glob.glob("/dev/input/js*")):
        try:
            result = subprocess.run(
                ["udevadm", "info", "-q", "property", path],
                capture_output=True, text=True, timeout=2
            )
            props = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            bus = props.get("ID_BUS")
            name = "Controller"
            js_id = os.path.basename(path).replace("js", "")
            name_path = f"/sys/class/input/js{js_id}/device/name"
            try:
                with open(name_path) as f:
                    name = f.read().strip() or name
            except Exception:
                pass
            usb_id = f"{props.get('ID_VENDOR_ID', '')}:{props.get('ID_MODEL_ID', '')}".lower()
            vidpid = None
            try:
                ids = f"/sys/class/input/js{js_id}/device/id/"
                with open(ids + "vendor") as fv, open(ids + "product") as fp:
                    vidpid = (int(fv.read(), 16), int(fp.read(), 16))
            except Exception:
                pass
            devices.append({"name": name, "bus": bus, "usb_id": usb_id, "vidpid": vidpid})
        except Exception:
            continue
    return devices

_GENERIC_NAME_TAIL = re.compile(r"(\s+(wireless|bluetooth|controller|gamepad|game\s*pad|joystick))+\s*$", re.I)

def short_controller_name(name):
    """Card label: the device name without generic trailing words, e.g.
    "8BitDo Ultimate 2C Wireless Controller" -> "8BitDo Ultimate 2C",
    "Google Stadia Controller" -> "Google Stadia". Never trims to nothing."""
    short = _GENERIC_NAME_TAIL.sub("", name).strip()
    return short or name

def label_controllers(devices):
    """Sets each device's card "label": its short name, plus #1, #2... when
    several pads share one (e.g. two of the same model), in connection order."""
    for dev in devices:
        dev["label"] = short_controller_name(dev["name"])
    counts = {}
    for dev in devices:
        counts[dev["label"]] = counts.get(dev["label"], 0) + 1
    seen = {}
    for dev in devices:
        if counts[dev["label"]] > 1:
            seen[dev["label"]] = seen.get(dev["label"], 0) + 1
            dev["label"] = f"{dev['label']} #{seen[dev['label']]}"
    return devices

def refresh_controller_status():
    global linux_controllers
    bt_batteries = {}  # lowercased name -> battery, so it matches by identity, not list position
    bt_macs = {}  # lowercased name -> MAC address
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Connected"], capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                mac = parts[1]
                name, battery = get_device_info(mac)
                bt_batteries[name.lower()] = battery
                bt_macs[name.lower()] = mac
    except Exception:
        pass

    devices = []
    for d in list_js_devices():
        if d["bus"] == "bluetooth":
            connection = "Bluetooth"
        else:  # a wireless pad on its own receiver shows the receiver's label, e.g. "2.4G"
            connection = cfg.get("controllers.wireless_dongles").get(d["usb_id"], "USB")
        battery = bt_batteries.get(d["name"].lower()) if connection == "Bluetooth" else None
        mac = bt_macs.get(d["name"].lower()) if connection == "Bluetooth" else None
        devices.append({"name": d["name"], "vidpid": d["vidpid"], "battery": battery,
                        "connection": connection, "mac": mac})
    linux_controllers = devices

def _guid_vidpid(guid):
    """(vendor, product) from an SDL joystick GUID, or None. Layout: 16-bit
    little-endian bus, crc, vendor, 0, product, ... as 32 hex chars."""
    try:
        return int(guid[10:12] + guid[8:10], 16), int(guid[18:20] + guid[16:18], 16)
    except (ValueError, TypeError):
        return None

def build_controller_cards():
    """One card per SDL controller, in connection order: SDL's friendly name
    (not the Bluetooth name, e.g. "StadiaZVNQ-6ccb") and its instance id for
    the input indicator, plus connection/battery from the Linux device with
    the same USB vendor:product (identical pads are paired in order). Linux
    devices SDL doesn't know still get a card under their Linux name."""
    unclaimed = list(linux_controllers)
    cards = []
    for iid in sorted(joysticks):
        j = joysticks[iid]
        try:
            name, vidpid = j.get_name(), _guid_vidpid(j.get_guid())
        except pygame.error:
            continue
        match = next((d for d in unclaimed if vidpid and d["vidpid"] == vidpid), None)
        if match:
            unclaimed.remove(match)
        cards.append({"name": name, "iid": iid,
                      "connection": match["connection"] if match else "USB",
                      "battery": match["battery"] if match else None,
                      "mac": match["mac"] if match else None})
    cards += [dict(d, iid=None) for d in unclaimed]
    return label_controllers(cards)

# --- Host reachability ---
host_reachable = None

host_running_app_id = 0  # Sunshine's "currentgame": app ID with a session running, 0 = none

host_pc_name = None  # the PC's own name, from serverinfo

def refresh_host_status():
    """Asks Sunshine's serverinfo (plain HTTP, no pairing needed) whether the
    host is up and which app, if any, has a session running."""
    global host_reachable, host_running_app_id, host_pc_name
    if not HOST:  # not set up yet - nothing to ask
        host_reachable, host_running_app_id = None, 0
        return
    try:
        url = f"http://{HOST_URL_HOST}:{HOST_PORT}/serverinfo?uniqueid=0123456789ABCDEF"
        with urllib.request.urlopen(url, timeout=2) as response:
            root = ET.fromstring(response.read())
        host_running_app_id = int(root.findtext("currentgame") or 0)
        host_pc_name = root.findtext("hostname") or host_pc_name
        host_reachable = True
    except Exception:
        host_running_app_id = 0
        try:  # not a readable serverinfo - at least say whether the port answers
            with socket.create_connection((HOST, HOST_PORT), timeout=1):
                host_reachable = True
        except Exception:
            host_reachable = False

def moonlight_env():
    """Environment for background moonlight-qt commands (list, pair, quit,
    --version): Qt's offscreen platform, and WITHOUT the launcher's own
    SDL_VIDEODRIVER=kmsdrm - `pair` starts SDL, which would otherwise try the
    DRM display this launcher holds. Streams (launch()) keep the full env."""
    env = {k: v for k, v in os.environ.items() if k != "SDL_VIDEODRIVER"}
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env

# --- Host app list (moonlight-qt list) ---
def load_apps_cache():
    """[(name, id or None), ...] from the cache, or None. Older caches are a
    plain list of names."""
    try:
        with open(APPS_CACHE_FILE) as f:
            data = json.load(f)
        apps = [(a, None) if isinstance(a, str) else (str(a[0]), a[1]) for a in data]
        return apps or None
    except Exception:
        return None

_cached = load_apps_cache() or [(name, None) for name in cfg.get("apps.fallback")]
host_apps = [name for name, _ in _cached]
host_app_ids = {name: app_id for name, app_id in _cached if app_id is not None}

def running_app_name():
    """Name of the app with a session running on the host, "another app" if
    its ID isn't in our list, or None if nothing is running."""
    if not (host_reachable and host_running_app_id):
        return None
    return next((n for n, i in host_app_ids.items() if i == host_running_app_id), "another app")
last_apps_fetch = 0.0

# Moonlight 6.2.0's `list` on an unpaired host: "Computer X has not been paired.
# Please open Moonlight to pair before retrieving games list." (exit 255)
NOT_PAIRED_TEXT = "has not been paired"
host_paired = None  # True / False once `moonlight-qt list` has told us, None = unknown

def _run_moonlight_list(args, timeout):
    """`moonlight-qt list [args] HOST`, or None if it failed or timed out.
    Qt must use the offscreen platform (by default it grabs the DRM display,
    which this launcher holds); /tmp as cwd keeps its eglfs_override files out of ~."""
    env = moonlight_env()
    try:
        return subprocess.run(["moonlight-qt", "list", *args, MOONLIGHT_HOST], capture_output=True, text=True,
                              timeout=timeout, env=env, cwd="/tmp", stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        def as_text(x):  # partial output can come back as bytes even with text=True
            return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        out = as_text(e.stdout) + as_text(e.stderr)
        print(f"moonlight-qt list {' '.join(args)} timed out after {e.timeout}s. Output so far:")
        print(out[-1500:] or "(none)")
        return None
    except Exception as e:
        print(f"moonlight-qt list failed: {e}")
        return None

def _list_lines(result):
    """Output lines that aren't Qt/Moonlight log lines ("00:00:01 - Qt Warning: ...")."""
    return [line.strip() for line in result.stdout.splitlines()
            if line.strip() and not re.match(r"\d\d:\d\d:\d\d - ", line)]

def fetch_host_apps():
    """App names via plain `moonlight-qt list` (fast), or None on failure.
    Needs Moonlight 6.2.0+: older versions print a stale cached list (issue #1).
    Not `--csv`: that first downloads any box art Moonlight hasn't cached, and
    in CLI mode (6.2.0) that hangs ("QEventLoop: Cannot be used without
    QApplication") - so IDs come separately, see fetch_app_ids()."""
    global host_paired
    result = _run_moonlight_list([], 30)
    if result is None:
        return None
    if NOT_PAIRED_TEXT in result.stdout + result.stderr:
        host_paired = False
        return None
    names = _list_lines(result)
    if result.returncode != 0 or not names:
        print(f"moonlight-qt list gave no apps (exit {result.returncode}): {result.stderr.strip()[-200:]}")
        return None
    host_paired = True
    return names

def fetch_app_ids():
    """{app name: Sunshine app ID} via `moonlight-qt list --csv`, or None. Can
    stall on uncached box art (see fetch_host_apps), so it has its own limit
    and only ever runs in the background. Opening Moonlight Window once caches
    the box art, after which it works."""
    result = _run_moonlight_list(["--csv"], 20)
    if result is None or result.returncode != 0:
        return None
    ids = {}
    for row in csv.reader(_list_lines(result), skipinitialspace=True):  # Name, ID, HDR Support, ...
        if row and row[0] != "Name":
            try:
                ids[row[0].strip()] = int(row[1])
            except (IndexError, ValueError):
                pass
    return ids or None

def save_apps_cache():
    try:
        with open(APPS_CACHE_FILE, "w") as f:
            json.dump([[name, host_app_ids.get(name)] for name in host_apps], f)
    except Exception:
        pass

_ids_fetching = False

def refresh_app_ids_background():
    """Updates host_app_ids (used to spot a running session) without ever
    blocking the app list: keeps the IDs already known if --csv stalls."""
    global _ids_fetching
    if _ids_fetching:
        return
    _ids_fetching = True

    def worker():
        global host_app_ids, _ids_fetching
        try:
            ids = fetch_app_ids()
            if ids:
                merged = {**host_app_ids, **ids}
                host_app_ids = {name: merged[name] for name in host_apps if name in merged}
                save_apps_cache()
        finally:
            _ids_fetching = False
    threading.Thread(target=worker, daemon=True).start()

def refresh_host_apps():
    """Re-reads the host's app names into host_apps (and the cache), then
    refreshes their IDs in the background. Returns the names, or None if the
    host couldn't be asked."""
    global host_apps, last_apps_fetch
    last_apps_fetch = time.time()
    names = fetch_host_apps()
    if not names:
        return None
    if names != host_apps:
        host_apps = names
        save_apps_cache()
    refresh_app_ids_background()
    return names

def reload_apps():
    """The Reload Apps button: fetch now instead of waiting for the next
    periodic refresh, and say what happened."""
    if not HOST:
        show_status_popup("No host set up yet - choose Set up host first")
        pygame.time.wait(2500)
        return
    show_status_popup("Reloading apps from host...")
    refresh_host_status()  # 1s check first - an unreachable host would make moonlight-qt wait much longer
    if not host_reachable:
        show_status_popup(f"Host unreachable - showing {len(host_apps)} saved apps")
        pygame.time.wait(2500)
        return
    apps = refresh_host_apps()
    pygame.event.clear()  # drop presses made while it was busy
    if apps is None:
        show_status_popup(f"Couldn't read apps - showing {len(host_apps)} saved apps")
    else:
        show_status_popup(f"Found {len(apps)} app{'s' if len(apps) != 1 else ''}")
    pygame.time.wait(2000)

# --- Moonlight installed? Lunapad only checks - installing it is up to you ---
MOONLIGHT_MIN_VERSION = (6, 2, 0)  # `moonlight-qt list` returns a fresh app list from 6.2.0 (issue #1)
moonlight_present = shutil.which("moonlight-qt") is not None
moonlight_version = None  # (major, minor, patch) once read
_version_checking = False

def check_moonlight_version():
    """Reads `moonlight-qt --version` (e.g. "Moonlight 6.2.0") in the background."""
    global moonlight_version, _version_checking
    try:
        result = subprocess.run(["moonlight-qt", "--version"], capture_output=True, text=True, timeout=15,
                                env=moonlight_env(), cwd="/tmp",
                                stdin=subprocess.DEVNULL)
        m = re.search(r"Moonlight\s+(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
        moonlight_version = tuple(int(x) for x in m.groups()) if m else None
    except Exception as e:
        print(f"moonlight-qt --version failed: {e}")
    finally:
        _version_checking = False

def refresh_moonlight_status():
    """Is moonlight-qt on the PATH, and which version? Re-checked with the
    host status, so installing (or removing) it shows up without a restart."""
    global moonlight_present, moonlight_version, _version_checking
    moonlight_present = shutil.which("moonlight-qt") is not None
    if not moonlight_present:
        moonlight_version = None
    elif moonlight_version is None and not _version_checking:
        _version_checking = True
        check_moonlight_version()

_status_refreshing = False

def _refresh_status_worker():
    global _status_refreshing
    try:
        refresh_controller_status()
        refresh_host_status()
        refresh_moonlight_status()
        # the app list only changes when you edit Sunshine - re-ask occasionally
        if host_reachable and time.time() - last_apps_fetch > cfg.get("apps.refresh_minutes") * 60:
            refresh_host_apps()
    finally:
        _status_refreshing = False

def refresh_status_background():
    """Runs the bluetoothctl/host checks off the UI thread - they can block for
    seconds (e.g. a 1s connect timeout when the host is asleep), which froze
    input every refresh. Skips if the previous refresh is still running."""
    global _status_refreshing
    if _status_refreshing:
        return
    _status_refreshing = True
    threading.Thread(target=_refresh_status_worker, daemon=True).start()

CONTROLLER_REFRESH_EVENT = pygame.USEREVENT + 1
refresh_status_background()

# --- Update checking (background thread, once/day) ---
pending_updates = 0
checking_updates = False

def load_update_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            return data.get("last_check", 0), data.get("pending", 0)
    except Exception:
        return 0, 0

def save_update_state(pending):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_check": time.time(), "pending": pending}, f)
    except Exception:
        pass

# apt must never wait for input - there's no keyboard. `sudo -n` fails instead
# of prompting for a password, stdin is /dev/null, and DEBIAN_FRONTEND stops
# debconf questions (kept through sudo by deploy/sudoers-lunapad). Config-file
# questions are answered by deploy/apt-lunapad.conf (keep the current file).
APT_ENV = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}

def check_updates_background():
    global pending_updates, checking_updates
    checking_updates = True
    try:
        subprocess.call(
            ["sudo", "-n", "/usr/bin/apt-get", "update"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=APT_ENV, timeout=600,
        )
        result = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True)
        lines = [l for l in result.stdout.splitlines() if "/" in l]
        pending_updates = len(lines)
        save_update_state(pending_updates)
    except Exception:
        pass
    checking_updates = False

last_check_time, pending_updates = load_update_state()
if time.time() - last_check_time > CHECK_INTERVAL:
    threading.Thread(target=check_updates_background, daemon=True).start()

UPDATE_CHECK_EVENT = pygame.USEREVENT + 2

# --- Idle controller disconnect ---
last_activity = time.time()
last_idle_disconnect = 0  # separate from last_activity, so disconnecting doesn't un-dim the screen

def disconnect_idle_controllers():
    """Gamepads only (bt_list_known_devices filters on Icon: input-gaming) -
    headphones, keyboards etc. stay connected."""
    for d in bt_list_known_devices():
        if d["connected"]:
            bt_disconnect(d["mac"])
            print(f"Disconnected idle controller: {d['name']} ({d['mac']})")

IDLE_CHECK_EVENT = pygame.USEREVENT + 3

def start_timers():
    """(Re)arms the periodic events - pygame.quit() in launch() clears them."""
    pygame.time.set_timer(CONTROLLER_REFRESH_EVENT, 5000)
    pygame.time.set_timer(UPDATE_CHECK_EVENT, 3600_000)  # re-check hourly if 24h elapsed
    pygame.time.set_timer(IDLE_CHECK_EVENT, 60_000)  # check every minute

start_timers()

# --- Idle screen dim (burn-in protection) ---
# Timings come from the idle.* settings (see screen_dim_alpha). Any input while dimmed wakes the screen and is swallowed (not acted on), so
# a stray press just wakes the display instead of also launching something.
def note_activity():
    global last_activity
    last_activity = time.time()

def screen_dim_alpha():
    """Two stages: after idle.dim_after_minutes, fade to idle.dim_level over
    idle.fade_seconds; after idle.black_after_minutes more, fade to black.
    0 minutes = never dim / stay dimmed. Read live, so the Settings page
    applies at once."""
    dim_after = cfg.get("idle.dim_after_minutes") * 60
    if not dim_after:
        return 0
    fade = max(1, cfg.get("idle.fade_seconds"))
    level = max(0, min(255, cfg.get("idle.dim_level")))
    t = time.time() - last_activity - dim_after
    if t <= 0:
        return 0
    if t < fade:  # fading to dimmed
        return int(t / fade * level)
    black_after = cfg.get("idle.black_after_minutes") * 60
    if not black_after:
        return level
    t -= fade + black_after
    if t <= 0:  # holding dimmed
        return level
    return min(255, int(level + t / fade * (255 - level)))  # fading to black

def is_screen_dimmed():
    return screen_dim_alpha() > 0

def draw_screen_dim():
    alpha = screen_dim_alpha()
    if alpha > 0:
        dim = pygame.Surface((w, h), pygame.SRCALPHA)
        dim.fill((0, 0, 0, alpha))
        screen.blit(dim, (0, 0))

def draw_panel(x, y, pw, ph):
    panel = pygame.Surface((pw, ph), pygame.SRCALPHA)
    pygame.draw.rect(panel, (*PANEL_BG, 210), panel.get_rect(), border_radius=20)
    screen.blit(panel, (x, y))

def draw_tile(rect, label, is_selected, danger=False):
    if is_selected:
        pygame.draw.rect(screen, ACCENT, rect, border_radius=16)
        text_color = (10, 10, 15)
    else:
        pygame.draw.rect(screen, TILE_BG, rect, border_radius=16)
        pygame.draw.rect(screen, DIVIDER, rect, width=1, border_radius=16)
        text_color = BAD if danger else TEXT
    text_surf = fit_text(label, (item_font, subtitle_font), text_color, rect.width - 40)
    screen.blit(text_surf, text_surf.get_rect(center=rect.center))
    return text_surf

def draw_clock(margin, title_y, title_h):
    """Date/time in the top-right corner, vertically centered on the page's
    title - shared by the dashboard and Bluetooth pages."""
    clock_surf = subtitle_font.render(time.strftime("%a %d %b   %H:%M"), True, MUTED)
    clock_rect = clock_surf.get_rect(topright=(w - margin, title_y + (title_h - clock_surf.get_height()) // 2))
    screen.blit(clock_surf, clock_rect)

def fit_text(text, fonts, color, max_w):
    """Renders `text` in the first of `fonts` (largest first) that fits
    `max_w`; if none do, shortens it with "..." in the last font."""
    for font in fonts:
        surf = font.render(text, True, color)
        if surf.get_width() <= max_w:
            return surf
    font = fonts[-1]
    while text and font.size(text + "...")[0] > max_w:
        text = text[:-1]
    return font.render(text.rstrip() + "...", True, color)

def draw_frame():
    global selected
    # The tiles above Moonlight Window: the host's apps, or - while this Pi
    # isn't paired - a single "Pair with Sunshine" tile instead.
    if not HOST:
        grid_keys = ["sethost"]
    elif host_paired is False:
        grid_keys = ["pair"]
    else:
        grid_keys = [app_key(app) for app in host_apps]
    # Start on the first tile, and fall back to it if the selected tile is gone
    # (an app vanished from the host's list, or the apps are hidden while unpaired).
    if selected not in set(grid_keys) | (set(NODES) - {"pair", "sethost"}):
        selected = grid_keys[0]
    if bg:
        screen.blit(bg, (0, 0))
        overlay = pygame.Surface((w, h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 140))
        screen.blit(overlay, (0, 0))
    else:
        screen.fill(THEME["bg"])

    margin = 60
    title = title_font.render("Lunapad", True, TEXT)
    screen.blit(title, (margin, 36))
    # the version, small and grey, sitting on the title's baseline
    version = hint_font.render(VERSION_SHORT, True, MUTED)
    baseline = 36 + title_font.get_ascent()
    screen.blit(version, (margin + title.get_width() + 18, baseline - hint_font.get_ascent()))
    draw_clock(margin, 36, title.get_height())

    panel_y = 150
    panel_h = 700
    left_w = 560
    right_x = margin + left_w + 40
    right_w = w - margin - right_x

    # --- Left panel: this device + connected controllers ---
    draw_panel(margin, panel_y, left_w, panel_h)
    pad = 30
    y = panel_y + pad
    device_surf = subtitle_font.render(f"IP: {CLIENT_IP}", True, MUTED)
    screen.blit(device_surf, (margin + pad, y))
    y += device_surf.get_height() + 18
    pygame.draw.line(screen, DIVIDER, (margin + pad, y), (margin + left_w - pad, y), 1)
    y += 22

    heading_surf = badge_font.render("CONTROLLERS CONNECTED", True, MUTED)
    screen.blit(heading_surf, (margin + pad, y))
    y += heading_surf.get_height() + 16

    # Compact fixed-height cards stacked from the top; any that don't fit are
    # clipped at the panel's bottom padding.
    cards_bottom = panel_y + panel_h - pad
    card_gap = 14
    card_h = 110
    card_x, card_w = margin + pad, left_w - pad * 2
    connected_controllers = build_controller_cards()
    if connected_controllers:
        name_max_w = card_w - 28 - 20
        name_font = subtitle_font
        prev_clip = screen.get_clip()
        screen.set_clip(pygame.Rect(card_x, y, card_w, cards_bottom - y))
        for c in connected_controllers:
            card_rect = pygame.Rect(card_x, y, card_w, card_h)
            pygame.draw.rect(screen, TILE_BG, card_rect, border_radius=12)
            pygame.draw.rect(screen, DIVIDER, card_rect, width=1, border_radius=12)
            # input indicator: outline flashes when this pad is used, fading out
            # over INPUT_FLASH_SECONDS
            age = time.time() - last_input_at.get(c["iid"], 0) if c.get("iid") is not None else INPUT_FLASH_SECONDS
            if age < INPUT_FLASH_SECONDS:
                glow = pygame.Surface(card_rect.size, pygame.SRCALPHA)
                alpha = int(255 * (1 - age / INPUT_FLASH_SECONDS))
                pygame.draw.rect(glow, (*ACCENT, alpha), glow.get_rect(), width=3, border_radius=12)
                screen.blit(glow, card_rect.topleft)
            accent = MUTED if c["connection"] == "USB" else ACCENT  # blue strip = wireless
            pygame.draw.rect(screen, accent, pygame.Rect(card_x, y, 6, card_h),
                             border_top_left_radius=12, border_bottom_left_radius=12)

            # Name on top, connection + battery underneath - centered as a block.
            name_surf = fit_text(c.get("label", c["name"]), (name_font,), TEXT, name_max_w)
            conn_surf = hint_font.render(c["connection"], True, MUTED)
            row_h = max(conn_surf.get_height(), 24)
            block_h = name_surf.get_height() + 10 + row_h
            text_x = card_x + 28
            top = card_rect.y + (card_h - block_h) // 2
            screen.blit(name_surf, (text_x, top))

            cy = top + name_surf.get_height() + 10 + row_h // 2
            cx = text_x
            if c["connection"] == "Bluetooth":
                draw_bluetooth_icon(screen, (cx + 12, cy), 24, ACCENT)
                cx += 32
            screen.blit(conn_surf, (cx, cy - conn_surf.get_height() // 2))
            cx += conn_surf.get_width() + 28

            if c["battery"]:
                cx += draw_battery_icon(screen, cx, cy - 7, int(c["battery"]), c.get("charging", False))
                batt_surf = hint_font.render(f"{c['battery']}%", True, MUTED)
                screen.blit(batt_surf, (cx - 2, cy - batt_surf.get_height() // 2))
            elif c["connection"] == "Bluetooth":  # USB/dongle pads never report a battery - show nothing
                cx += draw_battery_unknown_icon(screen, cx, cy - 7)
                q_surf = badge_font.render("?", True, WARN)
                screen.blit(q_surf, (cx - 2, cy - q_surf.get_height() // 2))
            y += card_h + card_gap
        screen.set_clip(prev_clip)
    else:
        empty_rect = pygame.Rect(card_x, y, card_w, card_h)
        pygame.draw.rect(screen, TILE_BG, empty_rect, border_radius=12)
        pygame.draw.rect(screen, DIVIDER, empty_rect, width=1, border_radius=12)
        msg = subtitle_font.render("No controllers connected", True, BAD)
        tip = hint_font.render("Pair one from Bluetooth below", True, MUTED)
        gap = 12
        top = empty_rect.centery - (msg.get_height() + gap + tip.get_height()) // 2
        screen.blit(msg, msg.get_rect(midtop=(empty_rect.centerx, top)))
        screen.blit(tip, tip.get_rect(midtop=(empty_rect.centerx, top + msg.get_height() + gap)))

    # --- Right panel: Moonlight host + app tile grid ---
    draw_panel(right_x, panel_y, right_w, panel_h)
    rpad = 30
    if host_reachable is True:
        host_status, host_color = "Reachable", GOOD
    elif host_reachable is False:
        host_status, host_color = "Unreachable", BAD
    else:
        host_status, host_color = "Checking...", MUTED
    host_head = badge_font.render("MOONLIGHT HOST", True, MUTED)
    screen.blit(host_head, (right_x + rpad, panel_y + rpad))
    running = running_app_name()
    if running:
        host_status, host_color = f"Running: {running}", ACCENT
    if host_reachable and host_paired is False:
        host_status, host_color = "Not paired", BAD
    if not HOST:
        host_status, host_color = "Host not set up - choose Set up host", BAD
    if not moonlight_present:
        host_status, host_color = "Moonlight not installed", BAD
    host_text = f"{HOST_DISPLAY} - {host_status}" if HOST else host_status
    host_line = subtitle_font.render(host_text, True, host_color)
    screen.blit(host_line, (right_x + rpad, panel_y + rpad + host_head.get_height() + 6))
    extra_h = 0
    if moonlight_present and moonlight_version and moonlight_version < MOONLIGHT_MIN_VERSION:
        old = ".".join(map(str, moonlight_version))
        need = ".".join(map(str, MOONLIGHT_MIN_VERSION))
        warn = hint_font.render(f"Moonlight {old} is installed - update to {need} or newer "
                                "so new host apps appear", True, WARN)
        screen.blit(warn, (right_x + rpad, panel_y + rpad + host_head.get_height() + 6
                           + host_line.get_height() + 6))
        extra_h = warn.get_height() + 6

    # Host apps two per row (an odd last one spans the row), then Moonlight
    # Window full width - all rows share the panel height, max 240px each.
    tile_rects.clear()
    grid_top = panel_y + rpad + host_head.get_height() + host_line.get_height() + extra_h + 30
    grid_x, grid_w = right_x + rpad, right_w - rpad * 2
    tile_gap = 24
    app_rows = (len(grid_keys) + 1) // 2
    rows = app_rows + 1
    tile_h = min(240, (panel_y + panel_h - rpad - grid_top - tile_gap * (rows - 1)) // rows)
    half_w = (grid_w - tile_gap) // 2
    for i, key in enumerate(grid_keys):
        row, col = divmod(i, 2)
        alone = i == len(grid_keys) - 1 and col == 0
        tile_rects[key] = pygame.Rect(grid_x + col * (half_w + tile_gap),
                                               grid_top + row * (tile_h + tile_gap),
                                               grid_w if alone else half_w, tile_h)
    tile_rects["moonlight"] = pygame.Rect(grid_x, grid_top + app_rows * (tile_h + tile_gap), grid_w, tile_h)
    for key, rect in tile_rects.items():
        draw_tile(rect, node_label(key), selected == key)
        if running and key == app_key(running):
            # the app with a session running on the host: outline + resume hint
            pygame.draw.rect(screen, ACCENT, rect, width=3, border_radius=16)
            hint_color = (10, 10, 15) if selected == key else ACCENT
            hint_surf = hint_font.render("RUNNING  -  A resume  -  B quit", True, hint_color)
            screen.blit(hint_surf, hint_surf.get_rect(midbottom=(rect.centerx, rect.bottom - 14)))

    # Settings: a chip at the top-right of the host panel
    chip_selected = selected == "settings"
    chip_surf = hint_font.render("Settings", True, (10, 10, 15) if chip_selected else TEXT)
    chip = pygame.Rect(0, 0, chip_surf.get_width() + 44, 46)
    chip.topright = (right_x + right_w - rpad, panel_y + rpad - 4)
    if chip_selected:
        pygame.draw.rect(screen, ACCENT, chip, border_radius=23)
    else:
        pygame.draw.rect(screen, TILE_BG, chip, border_radius=23)
        pygame.draw.rect(screen, DIVIDER, chip, width=1, border_radius=23)
    screen.blit(chip_surf, chip_surf.get_rect(center=chip.center))
    tile_rects["settings"] = chip

    # --- Bottom bar: system utilities ---
    bar_y = panel_y + panel_h + 30
    bar_h = h - bar_y - 40
    draw_panel(margin, bar_y, w - margin * 2, bar_h)
    util_head = badge_font.render("SYSTEM UTILITIES", True, MUTED)
    screen.blit(util_head, (margin + rpad, bar_y + 14))

    btn_y = bar_y + 14 + util_head.get_height() + 12
    btn_h = bar_h - (btn_y - bar_y) - 14
    btn_w = 290  # fits "Reload Apps" at full size; 5 buttons still fit the bar
    btn_gap = 20
    btn_x = margin + rpad
    for key in ("bluetooth", "update", "reload", "reboot", "exit"):
        rect = pygame.Rect(btn_x, btn_y, btn_w, btn_h)
        tile_rects[key] = rect
        draw_tile(rect, NODES[key]["label"], selected == key, danger=(key == "reboot"))
        if key == "update" and pending_updates > 0:
            badge_center = (rect.right - 22, rect.top + 22)
            pygame.draw.circle(screen, WARN, badge_center, 16)
            badge_text = badge_font.render(str(pending_updates), True, (30, 20, 0))
            screen.blit(badge_text, badge_text.get_rect(center=badge_center))
        btn_x += btn_w + btn_gap

    hint = hint_font.render("D-Pad / Stick: move   A: select   B: back", True, MUTED)
    hint_rect = hint.get_rect(midbottom=(w // 2, h - 12))
    screen.blit(hint, hint_rect)

    if status_message:
        msg = item_font.render(status_message, True, ACCENT)
        msg_rect = msg.get_rect(center=(w // 2, h - 90))
        screen.blit(msg, msg_rect)

def show_status(message):
    global status_message
    status_message = message
    draw_frame()
    pygame.display.flip()

def show_status_popup(message, draw_bg=None):
    """Like show_status, but the message sits in a dimmed popup box - for
    status updates that follow a confirm_popup, so the whole flow feels
    like one continuous dialog instead of dropping back to the plain frame."""
    draw_bg = draw_bg or draw_frame
    draw_bg()
    dim = pygame.Surface((w, h), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 170))
    screen.blit(dim, (0, 0))

    msg_surf = item_font.render(message, True, TEXT)
    pad_x, pad_y = 50, 40
    box_w = msg_surf.get_width() + pad_x * 2
    box_h = msg_surf.get_height() + pad_y * 2
    box = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
    pygame.draw.rect(box, (*PANEL_BG, 245), box.get_rect(), border_radius=24)
    box.blit(msg_surf, (pad_x, pad_y))
    screen.blit(box, ((w - box_w) // 2, (h - box_h) // 2))
    pygame.display.flip()

def launch(cmd):
    global screen
    pygame.quit()
    # Moonlight writes eglfs_override_XXXXXX.conf into its working dir and
    # leaves them behind - run it from /tmp so they don't pile up in ~.
    subprocess.call(cmd, cwd="/tmp")
    pygame.init()
    init_input()
    screen = pygame.display.set_mode(RESOLUTION or (0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    start_timers()
    note_activity()  # the stream was activity - don't come back already dimmed
    refresh_status_background()

def get_pending_update_list():
    """Package names with an available upgrade, via `apt list --upgradable`
    (no sudo needed - just reads the already-cached package lists)."""
    try:
        result = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    names = []
    for line in result.stdout.splitlines():
        if "/" in line and not line.startswith("Listing..."):
            names.append(line.split("/", 1)[0])
    return names

def get_autoremove_list():
    """Packages `apt-get autoremove` would remove - mostly old kernels after an
    upgrade. A simulation (-s), so no sudo needed."""
    try:
        result = subprocess.run(["apt-get", "-s", "autoremove"], capture_output=True, text=True,
                                timeout=30, env=APT_ENV)
    except Exception:
        return []
    return [line.split()[1] for line in result.stdout.splitlines() if line.startswith("Remv ")]

LOG_LINES_VISIBLE = 16

def run_apt_live(args, phase_label, log_lines):
    """Runs an apt-get command, streaming its output into a live-updating
    popup box (log_lines is shared/appended across phases so update+upgrade
    show as one continuous scroll rather than two separate resets)."""
    try:
        proc = subprocess.Popen(["sudo", "-n"] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, env=APT_ENV)
    except OSError as e:
        log_lines.append(f"Could not start apt-get: {e}")
        return -1
    draw_apt_log(phase_label, log_lines)
    pygame.display.flip()
    while True:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        line = line.rstrip("\n").strip()
        if line:
            log_lines.append(line)
            del log_lines[:-LOG_LINES_VISIBLE]
            draw_apt_log(phase_label, log_lines)
            pygame.display.flip()
        pygame.event.pump()  # keep the window responsive/alive while streaming
    proc.wait()
    return proc.returncode

def draw_apt_log(phase_label, log_lines, title_color=TEXT, footer=None):
    draw_frame()
    dim = pygame.Surface((w, h), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 170))
    screen.blit(dim, (0, 0))

    box_w = int(w * 0.7)
    box_h = int(h * 0.6)
    box_x = (w - box_w) // 2
    box_y = (h - box_h) // 2
    box = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
    pygame.draw.rect(box, (*PANEL_BG, 245), box.get_rect(), border_radius=24)

    pad = 30
    title_surf = item_font.render(phase_label, True, title_color)
    box.blit(title_surf, (pad, pad))

    log_y = pad + title_surf.get_height() + 16
    for line in log_lines[-LOG_LINES_VISIBLE:]:
        line_surf = hint_font.render(line[:110], True, MUTED)
        box.blit(line_surf, (pad, log_y))
        log_y += line_surf.get_height() + 4

    if footer:
        footer_surf = hint_font.render(footer, True, TEXT)
        box.blit(footer_surf, footer_surf.get_rect(bottomright=(box_w - pad, box_h - pad)))

    screen.blit(box, (box_x, box_y))
    draw_screen_dim()

def wait_for_dismiss(draw):
    """Redraws `draw()` until A or B is pressed."""
    stick_state = {}
    while True:
        for event in pygame.event.get():
            if handle_device_event(event):
                continue
            if input_action(event, stick_state) in ("a", "b"):
                return
        draw()
        pygame.display.flip()
        clock.tick(30)

def do_update():
    global pending_updates
    log_lines = []
    failed = None
    rc = run_apt_live(["/usr/bin/apt-get", "update"], "Updating package lists...", log_lines)
    if rc != 0:
        failed = f"Updating package lists failed (exit {rc})"
    else:
        rc = run_apt_live(["/usr/bin/apt-get", "full-upgrade", "-y"], "Installing updates...", log_lines)
        if rc != 0:
            failed = f"Installing updates failed (exit {rc})"
    pygame.event.clear()  # discard any input queued up while the update ran uninterruptibly
    # Recount rather than assume 0 - after a failure some updates are still pending.
    pending_updates = len(get_pending_update_list())
    save_update_state(pending_updates)
    if failed:
        # Keep the log up (it holds apt's error) until the user dismisses it.
        wait_for_dismiss(lambda: draw_apt_log(failed, log_lines, title_color=BAD, footer="A / B: close"))
        return
    offer_autoremove()
    if os.path.exists("/var/run/reboot-required"):
        show_status_popup("Update complete - reboot needed")
        pygame.time.wait(3000)
    else:
        show_status_popup("Update complete")
        pygame.time.wait(2000)

def offer_autoremove():
    """After an update: if some packages are no longer needed, offers to remove
    them (never silently: the list can be checked first). Old kernels pile up
    in the small boot partition, and a full one makes later kernel upgrades fail."""
    show_status_popup("Checking for unused packages...")
    pkgs = get_autoremove_list()
    pygame.event.clear()
    if not pkgs:
        return
    n = len(pkgs)
    remove_label = f"Remove {n} Unused"
    choice = choice_popup(f"{n} package(s) no longer needed", [remove_label, "Show List", "Keep"])
    if choice == "Show List":
        lines = pkgs[:20] + ([f"...and {n - 20} more"] if n > 20 else [])
        if confirm_popup("No longer needed", lines, confirm_label=remove_label, cancel_label="Keep"):
            choice = remove_label
    if choice != remove_label:
        return
    log_lines = []
    rc = run_apt_live(["/usr/bin/apt-get", "autoremove", "-y"], "Removing unused packages...", log_lines)
    pygame.event.clear()
    if rc != 0:
        msg = f"Removing unused packages failed (exit {rc})"
        wait_for_dismiss(lambda: draw_apt_log(msg, log_lines, title_color=BAD, footer="A / B: close"))
    else:
        show_status_popup(f"Removed {n} unused package(s)")
        pygame.time.wait(2000)

def confirm_popup(title_text, message_lines, confirm_label="Understood - Launch", cancel_label="Cancel", draw_bg=None):
    draw_bg = draw_bg or draw_frame
    choices = [confirm_label] + ([cancel_label] if cancel_label else [])  # cancel_label=None: one button
    choice = 0
    deciding = True
    result = False
    stick_state = {}
    while deciding:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                deciding = False
            if handle_device_event(event):
                continue
            action = input_action(event, stick_state)
            if action in ("left", "right"):
                choice = (choice + (1 if action == "right" else -1)) % len(choices)
            elif action == "a":
                result = (choice == 0)
                deciding = False
            elif action == "b":
                result = False
                deciding = False

        draw_bg()

        dim = pygame.Surface((w, h), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 170))
        screen.blit(dim, (0, 0))

        title_surf = item_font.render(title_text, True, TEXT)
        line_surfs = [subtitle_font.render(line, True, MUTED) for line in message_lines]
        btn_surfs = [item_font.render(label, True, TEXT) for label in choices]

        pad_x, pad_y = 50, 40
        line_gap = 12
        btn_pad = 20
        btn_gap = 30

        content_w = max(
            title_surf.get_width(),
            max((s.get_width() for s in line_surfs), default=0),
            sum(s.get_width() + btn_pad * 2 for s in btn_surfs) + btn_gap * (len(btn_surfs) - 1),
        )
        content_h = (
            title_surf.get_height() + 30
            + sum(s.get_height() + line_gap for s in line_surfs) + 20
            + max(s.get_height() for s in btn_surfs) + btn_pad
        )

        box_w = content_w + pad_x * 2
        box_h = content_h + pad_y * 2
        box_x = (w - box_w) // 2
        box_y = (h - box_h) // 2

        box = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
        pygame.draw.rect(box, (*PANEL_BG, 245), box.get_rect(), border_radius=24)
        pygame.draw.rect(box, ACCENT, box.get_rect(), width=2, border_radius=24)
        screen.blit(box, (box_x, box_y))

        cy = box_y + pad_y
        screen.blit(title_surf, (box_x + pad_x, cy))
        cy += title_surf.get_height() + 30

        for surf in line_surfs:
            screen.blit(surf, (box_x + pad_x, cy))
            cy += surf.get_height() + line_gap
        cy += 20

        btn_x = box_x + pad_x
        for i, label in enumerate(choices):
            surf = btn_surfs[i]
            rect = pygame.Rect(btn_x, cy, surf.get_width() + btn_pad * 2, surf.get_height() + btn_pad)
            if i == choice:
                pygame.draw.rect(screen, ACCENT, rect, border_radius=12)
                text_color = (10, 10, 15)
            else:
                pygame.draw.rect(screen, (40, 40, 50), rect, border_radius=12)
                text_color = TEXT
            surf = item_font.render(label, True, text_color)
            screen.blit(surf, (rect.x + btn_pad, rect.y + btn_pad // 2))
            btn_x += rect.width + btn_gap

        draw_screen_dim()
        pygame.display.flip()
        clock.tick(30)
    return result

def do_reboot():
    show_status("Rebooting...")
    pygame.time.wait(500)
    subprocess.call(["sudo", "-n", "/usr/sbin/reboot"], stdin=subprocess.DEVNULL)

# --- Bluetooth management page ---
def bt_run(args, timeout=15):
    try:
        return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None

def bt_list_known_devices():
    result = bt_run(["devices"])
    devices = []
    if result:
        for line in result.stdout.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                devices.append({"mac": parts[1], "name": parts[2]})
    gamepads = []
    for d in devices:
        d["paired"] = d["connected"] = d["trusted"] = False
        d["battery"] = None
        icon = None
        info = bt_run(["info", d["mac"]], timeout=5)
        if info:
            for line in info.stdout.splitlines():
                line = line.strip()
                if line.startswith("Paired:"):
                    d["paired"] = line.endswith("yes")
                elif line.startswith("Connected:"):
                    d["connected"] = line.endswith("yes")
                elif line.startswith("Trusted:"):
                    d["trusted"] = line.endswith("yes")
                elif line.startswith("Battery Percentage:") and "(" in line:
                    d["battery"] = line.split("(")[1].split(")")[0]
                elif line.startswith("Icon:"):
                    icon = line.split("Icon:", 1)[1].strip()
        # Only gamepads - confirmed empirically that both the Stadia and 8BitDo
        # controllers report Icon: input-gaming; anything else (lights, phones,
        # headsets, etc.) reports a different icon or none and is filtered out.
        if icon == "input-gaming":
            gamepads.append(d)
    gamepads.sort(key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))
    return gamepads

def bt_scan(duration=8):
    bt_run(["--timeout", str(duration), "scan", "on"], timeout=duration + 5)

def bt_pair_and_trust(mac):
    bt_run(["pair", mac], timeout=15)
    bt_run(["trust", mac], timeout=10)

def bt_connect(mac):
    bt_run(["connect", mac], timeout=15)

def bt_disconnect(mac):
    bt_run(["disconnect", mac], timeout=10)

def bt_remove(mac):
    bt_run(["remove", mac], timeout=10)

def choice_popup(title_text, options, draw_bg=None):
    """Like confirm_popup but for an arbitrary list of labeled actions.
    Returns the chosen label, or None if the user backed out with B/Escape."""
    draw_bg = draw_bg or draw_frame
    choice = 0
    deciding = True
    result = None
    stick_state = {}
    while deciding:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                deciding = False
            if handle_device_event(event):
                continue
            action = input_action(event, stick_state)
            if action in ("left", "right"):
                choice = (choice + (1 if action == "right" else -1)) % len(options)
            elif action == "a":
                result = options[choice]
                deciding = False
            elif action == "b":
                result = None
                deciding = False

        draw_bg()
        dim = pygame.Surface((w, h), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 170))
        screen.blit(dim, (0, 0))

        title_surf = item_font.render(title_text, True, TEXT)
        btn_surfs = [item_font.render(label, True, TEXT) for label in options]
        pad_x, pad_y = 50, 40
        btn_pad = 20
        btn_gap = 30

        content_w = max(title_surf.get_width(),
                         sum(s.get_width() + btn_pad * 2 for s in btn_surfs) + btn_gap * (len(btn_surfs) - 1))
        content_h = title_surf.get_height() + 30 + max(s.get_height() for s in btn_surfs) + btn_pad * 2

        box_w = content_w + pad_x * 2
        box_h = content_h + pad_y * 2
        box_x = (w - box_w) // 2
        box_y = (h - box_h) // 2

        box = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
        pygame.draw.rect(box, (*PANEL_BG, 245), box.get_rect(), border_radius=24)
        box.blit(title_surf, (pad_x, pad_y))

        btn_y = pad_y + title_surf.get_height() + 30
        btn_x = pad_x
        for i, (label, surf) in enumerate(zip(options, btn_surfs)):
            btn_w = surf.get_width() + btn_pad * 2
            btn_h = surf.get_height() + btn_pad
            rect = pygame.Rect(btn_x, btn_y, btn_w, btn_h)
            if i == choice:
                pygame.draw.rect(box, ACCENT, rect, border_radius=14)
                text_color = (10, 10, 15)
            else:
                pygame.draw.rect(box, TILE_BG, rect, border_radius=14)
                pygame.draw.rect(box, DIVIDER, rect, width=1, border_radius=14)
                text_color = TEXT
            label_surf = item_font.render(label, True, text_color)
            box.blit(label_surf, label_surf.get_rect(center=rect.center))
            btn_x += btn_w + btn_gap

        screen.blit(box, (box_x, box_y))
        draw_screen_dim()
        pygame.display.flip()
        clock.tick(30)

    return result

def run_bluetooth_page():
    active = True
    stick_state = {}
    devices = bt_list_known_devices()
    selected_idx = 0
    scroll_offset = 0
    scanning = False
    scan_result = {}

    margin = 60
    list_y = 150
    panel_h = h - list_y - 70
    pad = 30
    row_h = 84
    row_gap = 14
    visible_rows = max(1, (panel_h - pad * 2 + row_gap) // (row_h + row_gap))

    def clamp_scroll():
        nonlocal scroll_offset
        if selected_idx < scroll_offset:
            scroll_offset = selected_idx
        elif selected_idx >= scroll_offset + visible_rows:
            scroll_offset = selected_idx - visible_rows + 1
        scroll_offset = max(0, min(scroll_offset, max(0, len(devices) - visible_rows)))

    def refresh():
        nonlocal devices, selected_idx
        devices = bt_list_known_devices()
        if devices:
            selected_idx = min(selected_idx, len(devices) - 1)
        else:
            selected_idx = 0
        clamp_scroll()

    def scan_worker():
        bt_scan(8)
        scan_result["devices"] = bt_list_known_devices()

    def render_bg():
        screen.fill(THEME["bg"])
        title = title_font.render("Bluetooth", True, TEXT)
        screen.blit(title, (margin, 36))
        draw_clock(margin, 36, title.get_height())
        note = hint_font.render(
            "A: connect/pair/manage   Y: scan for new gamepads   B: back", True, MUTED
        )
        screen.blit(note, (margin, 36 + title.get_height() + 8))

        list_w = w - margin * 2
        draw_panel(margin, list_y, list_w, panel_h)

        if not devices:
            msg = item_font.render("No known gamepads - press Y to scan", True, MUTED)
            screen.blit(msg, msg.get_rect(center=(w // 2, list_y + panel_h // 2)))
        else:
            panel_rect = pygame.Rect(margin, list_y, list_w, panel_h)
            prev_clip = screen.get_clip()
            screen.set_clip(panel_rect)
            ry = list_y + pad
            visible = devices[scroll_offset:scroll_offset + visible_rows]
            for i, d in enumerate(visible, start=scroll_offset):
                row_rect = pygame.Rect(margin + pad, ry, list_w - pad * 2, row_h)
                if i == selected_idx:
                    pygame.draw.rect(screen, ACCENT, row_rect, border_radius=14)
                    name_color = (10, 10, 15)
                    mac_color = (40, 40, 45)
                else:
                    pygame.draw.rect(screen, TILE_BG, row_rect, border_radius=14)
                    pygame.draw.rect(screen, DIVIDER, row_rect, width=1, border_radius=14)
                    name_color = TEXT
                    mac_color = MUTED

                name_surf = subtitle_font.render(d["name"], True, name_color)
                screen.blit(name_surf, (row_rect.x + 20, row_rect.y + 12))
                mac_surf = hint_font.render(d["mac"], True, mac_color)
                screen.blit(mac_surf, (row_rect.x + 20, row_rect.y + 12 + name_surf.get_height() + 4))

                if d["connected"]:
                    status_surf = badge_font.render("Connected", True, GOOD)
                elif d["paired"]:
                    status_surf = badge_font.render("Paired", True, mac_color)
                else:
                    status_surf = badge_font.render("Available", True, mac_color)
                cursor_x = row_rect.right - 20 - status_surf.get_width()
                screen.blit(status_surf, (cursor_x, row_rect.centery - status_surf.get_height() // 2))

                if d["battery"]:
                    cursor_x -= 16  # gap before the status label
                    batt_surf = hint_font.render(f"{d['battery']}%", True, mac_color)
                    cursor_x -= batt_surf.get_width()
                    screen.blit(batt_surf, (cursor_x, row_rect.centery - batt_surf.get_height() // 2))
                    cursor_x -= 6  # gap between icon and its label
                    cursor_x -= 31  # battery body (28) + nub (3)
                    draw_battery_icon(screen, cursor_x, row_rect.centery - 7, int(d["battery"]))

                ry += row_h + row_gap
            screen.set_clip(prev_clip)

            if len(devices) > visible_rows:
                track_x = margin + list_w - 14
                track_rect = pygame.Rect(track_x, list_y + pad, 6, panel_h - pad * 2)
                pygame.draw.rect(screen, TILE_BG, track_rect, border_radius=3)
                thumb_h = max(30, int(track_rect.height * visible_rows / len(devices)))
                thumb_y = track_rect.y + int((track_rect.height - thumb_h) * scroll_offset / max(1, len(devices) - visible_rows))
                pygame.draw.rect(screen, ACCENT, pygame.Rect(track_x, thumb_y, 6, thumb_h), border_radius=3)

                ry += row_h + row_gap

    while active:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                active = False
            if handle_device_event(event):
                continue
            action = input_action(event, stick_state)
            if action in ("up", "down") and devices:
                selected_idx = (selected_idx + (1 if action == "down" else -1)) % len(devices)
                clamp_scroll()
            elif action in ("b", "home"):
                active = False
            elif action == "y" and not scanning:
                scanning = True
                scan_result.clear()
                threading.Thread(target=scan_worker, daemon=True).start()
            elif action == "a" and devices and not scanning:
                d = devices[selected_idx]
                if d["connected"]:
                    options = ["Disconnect", "Forget", "Cancel"]
                elif d["paired"]:
                    options = ["Connect", "Forget", "Cancel"]
                else:
                    options = ["Pair & Connect", "Cancel"]
                choice = choice_popup(d["name"], options, draw_bg=render_bg)
                if choice == "Disconnect":
                    show_status_popup(f"Disconnecting {d['name']}...", draw_bg=render_bg)
                    bt_disconnect(d["mac"])
                    refresh()
                elif choice == "Connect":
                    show_status_popup(f"Connecting {d['name']}...", draw_bg=render_bg)
                    bt_connect(d["mac"])
                    refresh()
                elif choice == "Pair & Connect":
                    show_status_popup(f"Pairing with {d['name']}...", draw_bg=render_bg)
                    bt_pair_and_trust(d["mac"])
                    bt_connect(d["mac"])
                    refresh()
                elif choice == "Forget":
                    if confirm_popup(f"Forget {d['name']}?", ["This removes the pairing."],
                                      confirm_label="Forget", cancel_label="Cancel", draw_bg=render_bg):
                        show_status_popup(f"Forgetting {d['name']}...", draw_bg=render_bg)
                        bt_remove(d["mac"])
                        refresh()

        if scanning and "devices" in scan_result:
            devices = scan_result["devices"]
            selected_idx = min(selected_idx, max(len(devices) - 1, 0))
            clamp_scroll()
            scanning = False

        render_bg()
        if scanning:
            dim = pygame.Surface((w, h), pygame.SRCALPHA)
            dim.fill((0, 0, 0, 140))
            screen.blit(dim, (0, 0))
            msg = item_font.render("Scanning for gamepads...", True, TEXT)
            screen.blit(msg, msg.get_rect(center=(w // 2, h // 2)))
        draw_screen_dim()
        pygame.display.flip()
        clock.tick(30)

# --- Settings page ---
SETTINGS_ROW_H = 64
SETTINGS_HEADER_H = 56
SETTINGS_LIST_Y = 150

class _PairAction:
    """A Settings-page row that runs pairing instead of holding a value."""
    key, label, kind, restart = "action.pair", "Pair with host", "action", False
    help = "Pair this Pi with the host above (shows a PIN to enter in Sunshine)."

def settings_rows():
    """[("header", title) or ("setting", Setting), ...] - every on-page setting,
    grouped under its section title, in schema order."""
    rows, last = [], None
    for st in cfg.SCHEMA:
        if not st.page:
            continue
        if st.section != last:
            rows.append(("header", st.section))
            last = st.section
        rows.append(("setting", st))
        if st.key == "host.port":  # pairing belongs with the host it pairs to
            rows.append(("setting", _PairAction))
    return rows

def settings_row_tops(rows):
    tops, y = [], 0
    for kind, _ in rows:
        tops.append(y)
        y += SETTINGS_HEADER_H if kind == "header" else SETTINGS_ROW_H
    return tops, y

def settings_list_rect():
    panel_h = h - SETTINGS_LIST_Y - 70
    return pygame.Rect(60 + 30, SETTINGS_LIST_Y + 20, w - 120 - 60, panel_h - 20 - 90)

def _draw_arrow(center, direction, color):
    cx, cy = center
    d = 8 if direction == "right" else -8
    pygame.draw.polygon(screen, color, [(cx - d, cy - 9), (cx - d, cy + 9), (cx + d, cy)])

def draw_settings_page(rows, sel, scroll):
    screen.fill(THEME["bg"])
    margin = 60
    title = title_font.render("Settings", True, TEXT)
    screen.blit(title, (margin, 36))
    draw_clock(margin, 36, title.get_height())
    shown_path = cfg.CONFIG_FILE.replace(cfg.HOME, "~", 1)
    note = fit_text(f"{VERSION_TEXT}  -  saved in {shown_path} (other settings can be edited there)",
                    (hint_font,), MUTED, w - margin * 2)
    screen.blit(note, (margin, 36 + title.get_height() + 8))
    panel_h = h - SETTINGS_LIST_Y - 70
    draw_panel(margin, SETTINGS_LIST_Y, w - margin * 2, panel_h)

    view = settings_list_rect()
    tops, total_h = settings_row_tops(rows)
    if total_h > view.height:  # scroll bar: there's more above/below
        track = pygame.Rect(view.right + 12, view.y, 6, view.height)
        pygame.draw.rect(screen, TILE_BG, track, border_radius=3)
        thumb_h = max(40, track.height * view.height // total_h)
        thumb_y = track.y + (track.height - thumb_h) * scroll // max(1, total_h - view.height)
        pygame.draw.rect(screen, ACCENT, (track.x, thumb_y, track.width, thumb_h), border_radius=3)
    prev_clip = screen.get_clip()
    screen.set_clip(view)
    for i, (kind, item) in enumerate(rows):
        y = view.y + tops[i] - scroll
        if y > view.bottom or y + SETTINGS_ROW_H < view.y:
            continue
        if kind == "header":
            head = badge_font.render(cfg.SECTION_TITLES.get(item, item).upper(), True, MUTED)
            head_y = y + SETTINGS_HEADER_H - head.get_height() - 12
            screen.blit(head, (view.x, head_y))
            if item in cfg.SECTION_NOTES:  # e.g. "these override Moonlight's own settings"
                note = hint_font.render(cfg.SECTION_NOTES[item], True, WARN)
                screen.blit(note, (view.x + head.get_width() + 24,
                                   head_y + head.get_height() - note.get_height()))
            continue
        row = pygame.Rect(view.x, y, view.width, SETTINGS_ROW_H - 8)
        selected = i == sel
        if selected:
            pygame.draw.rect(screen, ACCENT, row, border_radius=12)
        fg = (10, 10, 15) if selected else TEXT
        label = subtitle_font.render(item.label, True, fg)
        screen.blit(label, (row.x + 20, row.centery - label.get_height() // 2))
        if item.kind == "action":  # pairing: show the current state
            value_text = {True: "Paired", False: "Not paired"}.get(host_paired, "Press A")
            value = subtitle_font.render(value_text, True, fg if selected else (BAD if host_paired is False else ACCENT))
        else:
            value = subtitle_font.render(cfg.value_label(item.key), True, fg if selected else ACCENT)
        if item.kind in ("choice", "bool"):  # < value >  - left/right change it
            right_x = row.right - 24
            _draw_arrow((right_x, row.centery), "right", fg if selected else MUTED)
            vx = right_x - 24 - value.get_width()
            _draw_arrow((vx - 24, row.centery), "left", fg if selected else MUTED)
        else:  # host address / port: A opens the keyboard
            vx = row.right - 24 - value.get_width()
        screen.blit(value, (vx, row.centery - value.get_height() // 2))
    screen.set_clip(prev_clip)

    item = rows[sel][1]
    help_text = item.help + ("  Applies after a launcher restart." if item.restart else "")
    help_surf = hint_font.render(help_text, True, MUTED)
    screen.blit(help_surf, (view.x, SETTINGS_LIST_Y + panel_h - 30 - help_surf.get_height()))
    hint = hint_font.render("Up/Down: choose   Left/Right: change   A: change / type   B: back", True, MUTED)
    screen.blit(hint, hint.get_rect(midbottom=(w // 2, h - 12)))
    draw_screen_dim()

# On-screen keyboards. Special keys: "BKSP", "DONE", and "SWITCH", which
# toggles between the full keyboard and the number pad (labelled "123" on the
# full keyboard and "ABC" on the number pad, like a phone).
HOST_FULL = [list("1234567890"), list("qwertyuiop"), list("asdfghjkl-"),
             list("zxcvbnm.") + ["BKSP"], ["SWITCH", "DONE"]]
# Number pads are a strict 3-column grid: a key can be (key, width in columns),
# and a single-width BKSP is drawn as a backspace icon instead of the word.
HOST_NUMPAD = [list("123"), list("456"), list("789"), [".", "0", ("BKSP", 1)],
               [("SWITCH", 1), ("DONE", 2)]]
PORT_NUMPAD = [list("123"), list("456"), list("789"), [("0", 2), ("BKSP", 1)], [("DONE", 3)]]
_KEY_WIDTHS = {"BKSP": 2, "DONE": 3, "SWITCH": 2}  # widths (in key columns) on the full keyboard

def _key(k):
    return k[0] if isinstance(k, tuple) else k

def _key_units(k):
    return k[1] if isinstance(k, tuple) else _KEY_WIDTHS.get(k, 1)

def _draw_backspace_icon(center, color):
    """A left-pointing key outline with an x in it - the usual "delete backwards" sign."""
    cx, cy = center
    hw, hh = 22, 15  # half width / height of the key shape
    pts = [(cx - hw - 10, cy), (cx - hw + 4, cy - hh), (cx + hw, cy - hh), (cx + hw, cy + hh), (cx - hw + 4, cy + hh)]
    pygame.draw.polygon(screen, color, pts, width=3)
    for dx in (-1, 1):
        pygame.draw.line(screen, color, (cx + 4 - 7, cy - 7 * dx), (cx + 4 + 7, cy + 7 * dx), 3)

def keyboard_popup(title_text, text, layouts, max_len, validate, draw_bg, start=0):
    """Text entry with the D-pad over an on-screen key grid. `layouts` is one
    key grid, or several the SWITCH key (and Y) cycles through, starting at
    index `start`. A types the highlighted key, X = backspace, RB = "." (when
    the keyboard has one), Start = done, B = cancel. A USB keyboard also works (type, Backspace, Enter, Esc).
    Returns the text, or None if cancelled. `validate(text)` returns an error
    message, or None if OK."""
    allowed = {_key(k) for layout in layouts for row in layout for k in row if len(_key(k)) == 1}
    which, row, col = start, 0, 0
    stick_state, error = {}, None
    key_w, key_h, gap = 84, 64, 10

    def key_rects(layout):
        """{(row, col): Rect} for a grid, each row centred."""
        rects, y = {}, 0
        for r, keys in enumerate(layout):
            widths = [key_w * _key_units(k) + gap * (_key_units(k) - 1) for k in keys]
            x = -(sum(widths) + gap * (len(keys) - 1)) // 2
            for c, kw in enumerate(widths):
                rects[(r, c)] = pygame.Rect(x, y, kw, key_h)
                x += kw + gap
            y += key_h + gap
        return rects

    all_rects = [key_rects(layout) for layout in layouts]
    grid_h = max(len(layout) for layout in layouts) * (key_h + gap) - gap  # box size stays put on switch

    def switch_label():
        """The SWITCH key names the layout it switches TO."""
        nxt = layouts[(which + 1) % len(layouts)]
        return "ABC" if any(_key(k).isalpha() and len(_key(k)) == 1 for r in nxt for k in r) else "123"

    def press(key):
        nonlocal text, error, which, row, col
        error = None
        if key == "BKSP":
            text = text[:-1]
        elif key == "DONE":
            error = validate(text)
            return error is None
        elif key == "SWITCH":
            which, row, col = (which + 1) % len(layouts), 0, 0
        elif len(text) < max_len:
            text += key
        return False

    while True:
        layout, rects = layouts[which], all_rects[which]
        for event in pygame.event.get():
            if event.type == pygame.KEYDOWN:  # a real keyboard
                if event.key == pygame.K_RETURN:
                    if press("DONE"):
                        return text
                    continue
                if event.key == pygame.K_BACKSPACE:
                    press("BKSP")
                    continue
                if event.unicode and event.unicode.lower() in allowed:
                    press(event.unicode.lower())
                    continue
            if handle_device_event(event):
                continue
            layout, rects = layouts[which], all_rects[which]
            action = input_action(event, stick_state)
            if action in ("left", "right"):
                col = (col + (1 if action == "right" else -1)) % len(layout[row])
            elif action in ("up", "down"):
                # move to the key in the next row nearest the current one's centre
                cx = rects[(row, col)].centerx
                row = (row + (1 if action == "down" else -1)) % len(layout)
                col = min(range(len(layout[row])), key=lambda c: abs(rects[(row, c)].centerx - cx))
            elif action == "a":
                if press(_key(layout[row][col])):
                    return text
            elif action == "x":
                press("BKSP")
            elif action == "y" and len(layouts) > 1:
                press("SWITCH")
            elif action == "rb" and "." in allowed:  # shortcut for typing an IP
                press(".")
            elif action == "start":
                if press("DONE"):
                    return text
            elif action in ("b", "home"):
                return None
        layout, rects = layouts[which], all_rects[which]

        draw_bg()
        dim = pygame.Surface((w, h), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 170))
        screen.blit(dim, (0, 0))
        box = pygame.Rect(0, 0, 1180, 330 + grid_h)
        box.center = (w // 2, h // 2)
        pygame.draw.rect(screen, PANEL_BG, box, border_radius=24)
        title_surf = item_font.render(title_text, True, TEXT)
        screen.blit(title_surf, title_surf.get_rect(midtop=(box.centerx, box.y + 26)))

        field = pygame.Rect(box.x + 60, box.y + 100, box.width - 120, 70)
        pygame.draw.rect(screen, TILE_BG, field, border_radius=12)
        pygame.draw.rect(screen, ACCENT, field, width=2, border_radius=12)
        shown = fit_text(text, (item_font,), TEXT, field.width - 60)
        screen.blit(shown, shown.get_rect(midleft=(field.x + 20, field.centery)))
        if (pygame.time.get_ticks() // 500) % 2 == 0:  # blinking caret
            cx = field.x + 22 + (shown.get_width() if text else 0)
            pygame.draw.line(screen, ACCENT, (cx, field.y + 14), (cx, field.bottom - 14), 3)

        origin = (box.centerx, field.bottom + 30)
        for (r, c), rect in rects.items():
            kr = rect.move(*origin)
            selected = (r, c) == (row, col)
            key, units = _key(layout[r][c]), _key_units(layout[r][c])
            if selected:
                pygame.draw.rect(screen, ACCENT, kr, border_radius=10)
            else:
                pygame.draw.rect(screen, TILE_BG, kr, border_radius=10)
                pygame.draw.rect(screen, DIVIDER, kr, width=1, border_radius=10)
            fg = (10, 10, 15) if selected else TEXT
            if key == "BKSP" and units == 1:  # number pads: icon, keeps the grid neat
                _draw_backspace_icon(kr.center, fg)
                continue
            label = {"BKSP": "Backspace", "DONE": "Done", "SWITCH": switch_label()}.get(key, key)
            surf = (subtitle_font if len(label) > 1 else item_font).render(label, True, fg)
            screen.blit(surf, surf.get_rect(center=kr.center))

        hint = "A: type   X: backspace   " + (f"Y: {switch_label()}   " if len(layouts) > 1 else "") \
            + ("RB: .   " if "." in allowed else "") + "Start: done   B: cancel"
        msg = hint_font.render(error or hint, True, BAD if error else MUTED)
        screen.blit(msg, msg.get_rect(midbottom=(box.centerx, box.bottom - 24)))
        draw_screen_dim()
        pygame.display.flip()
        clock.tick(30)

def edit_setting_text(st, draw_bg):
    """Keyboard entry for a host/port setting. Returns the new value or None."""
    if st.kind == "port":
        def validate(t):
            return None if t.isdigit() and 1 <= int(t) <= 65535 else "Enter a port from 1 to 65535"
        text = keyboard_popup(st.label, str(cfg.get(st.key)), [PORT_NUMPAD], 5, validate, draw_bg)
        return int(text) if text is not None else None
    def validate(t):
        return None if t and cfg._valid(st, t) else "Enter a hostname (letters, digits, - and .) or an IP"
    current = cfg.get(st.key)
    # open on the number pad for an IP (or nothing yet), the full keyboard for a hostname
    start = 1 if re.fullmatch(r"[0-9.]*", current) else 0
    return keyboard_popup(st.label, current, [HOST_FULL, HOST_NUMPAD], 253, validate, draw_bg, start=start)

def run_settings_page():
    rows = settings_rows()
    selectable = [i for i, (kind, _) in enumerate(rows) if kind == "setting"]
    sel, scroll, stick_state = selectable[0], 0, {}
    tops, total_h = settings_row_tops(rows)

    def keep_visible():
        nonlocal scroll
        view_h = settings_list_rect().height
        top = tops[sel]
        # show the section header above the first setting of a section
        if sel > 0 and rows[sel - 1][0] == "header":
            top = tops[sel - 1]
        if top < scroll:
            scroll = top
        elif tops[sel] + SETTINGS_ROW_H > scroll + view_h:
            scroll = tops[sel] + SETTINGS_ROW_H - view_h
        scroll = max(0, min(scroll, max(0, total_h - view_h)))

    def change(step):
        st = rows[sel][1]
        if st.kind == "action":
            run_pairing(lambda: draw_settings_page(rows, sel, scroll))
        elif st.kind in ("host", "port"):
            new = edit_setting_text(st, lambda: draw_settings_page(rows, sel, scroll))
            if new is not None:
                cfg.set(st.key, new)
        else:
            cfg.set(st.key, cfg.cycle(st.key, step))

    active = True
    while active:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                active = False
            if handle_device_event(event):
                continue
            action = input_action(event, stick_state)
            if action in ("up", "down"):
                # wraps: up on the first setting goes to the last, and vice versa
                pos = selectable.index(sel) + (1 if action == "down" else -1)
                sel = selectable[pos % len(selectable)]
                keep_visible()
            elif action in ("left", "right") and rows[sel][1].kind in ("choice", "bool"):
                change(1 if action == "right" else -1)
            elif action == "a":
                change(1)
            elif action in ("b", "home"):
                active = False
        draw_settings_page(rows, sel, scroll)
        pygame.display.flip()
        clock.tick(30)

    changed = [cfg.BY_KEY[k].label for k, v in RESTART_SETTINGS_AT_START.items() if cfg.get(k) != v]
    if changed and confirm_popup("Restart to apply?", [f"Changed: {', '.join(changed)}",
                                                       "The launcher restarts in a couple of seconds."],
                                 confirm_label="Restart now", cancel_label="Later",
                                 draw_bg=lambda: draw_settings_page(rows, sel, scroll)):
        show_status_popup("Restarting...")
        pygame.time.wait(500)
        pygame.quit()
        sys.exit(0)  # not 99: the tty1 loop starts the launcher again

def quit_running_app(name):
    """Ends the host's running session (`moonlight-qt quit`). True if the
    host then reports nothing running."""
    show_status_popup(f"Closing {name}...")
    env = moonlight_env()
    try:
        subprocess.run(["moonlight-qt", "quit", MOONLIGHT_HOST], capture_output=True, text=True,
                       timeout=30, env=env, cwd="/tmp", stdin=subprocess.DEVNULL)
    except Exception as e:
        print(f"moonlight-qt quit failed: {e}")
    refresh_host_status()
    pygame.event.clear()  # drop presses made while it was busy
    if host_running_app_id:
        show_status_popup(f"Couldn't close {name}")
        pygame.time.wait(2500)
        return False
    return True

PAIR_TIMEOUT = 180  # seconds to enter the PIN; `pair` can hang (e.g. if already paired)

def moonlight_list_status(target):
    """("paired" | "not paired" | "unreachable", apps or None) for a host, via
    `moonlight-qt list`. `target` is a moonlight host string (host or host:port)."""
    try:
        result = subprocess.run(["moonlight-qt", "list", target], capture_output=True, text=True,
                                timeout=30, env=moonlight_env(),
                                cwd="/tmp", stdin=subprocess.DEVNULL)
    except Exception:
        return "unreachable", None
    if NOT_PAIRED_TEXT in result.stdout + result.stderr:
        return "not paired", None
    return ("paired", result.stdout) if result.returncode == 0 else ("unreachable", None)

def pairing_popup(lines, pin=None, footer=None, draw_bg=None):
    (draw_bg or draw_frame)()
    dim = pygame.Surface((w, h), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 170))
    screen.blit(dim, (0, 0))
    box = pygame.Rect(0, 0, 1180, 560 if pin else 300)
    box.center = (w // 2, h // 2)
    pygame.draw.rect(screen, PANEL_BG, box, border_radius=24)
    y = box.y + 36
    for i, line in enumerate(lines):
        surf = (item_font if i == 0 else subtitle_font).render(line, True, TEXT if i == 0 else MUTED)
        screen.blit(surf, surf.get_rect(midtop=(box.centerx, y)))
        y += surf.get_height() + (22 if i == 0 else 10)
    if pin:
        pin_font = pygame.font.SysFont(None, 200)
        pin_surf = pin_font.render("  ".join(pin), True, ACCENT)
        screen.blit(pin_surf, pin_surf.get_rect(midtop=(box.centerx, y + 20)))
    if footer:
        surf = hint_font.render(footer, True, MUTED)
        screen.blit(surf, surf.get_rect(midbottom=(box.centerx, box.bottom - 28)))
    draw_screen_dim()
    pygame.display.flip()

def run_pairing(draw_bg=None):
    """Pairs this Pi with the host from Settings: shows a PIN to enter in
    Sunshine and runs `moonlight-qt pair` in the background. Success is
    confirmed with `moonlight-qt list` - `pair` can hang instead of failing
    (it waits on a dialog nobody sees), so it's cancellable (B) and capped
    at PAIR_TIMEOUT. Returns True if the Pi is paired afterwards."""
    global host_paired, last_apps_fetch
    if not cfg.get("host.address"):
        confirm_popup("Pairing", ["Set the host address first (Settings > Host address)."],
                      confirm_label="OK", cancel_label=None, draw_bg=draw_bg)
        return False
    if not require_moonlight():
        return False
    target = cfg.moonlight_host()  # the host as it's set now, even before a restart
    name = host_pc_name if target == MOONLIGHT_HOST and host_pc_name else cfg.get("host.address")

    pairing_popup([f"Checking {name}..."], draw_bg=draw_bg)
    status, _ = moonlight_list_status(target)
    if status != "not paired":
        message = {"paired": f"This Pi is already paired with {name}.",
                   "unreachable": f"Couldn't reach {name} - is it on, with Sunshine running?"}[status]
        if status == "paired" and target == MOONLIGHT_HOST:
            host_paired = True
        confirm_popup("Pairing", [message], confirm_label="OK", cancel_label=None, draw_bg=draw_bg)
        return status == "paired"

    pin = f"{random.randint(0, 9999):04d}"
    proc = subprocess.Popen(["moonlight-qt", "pair", target, "--pin", pin],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            env=moonlight_env(), cwd="/tmp")
    # Don't rely on `pair` exiting - it can sit on a hidden dialog. A background
    # thread asks `list` every few seconds; as soon as it works, we're paired.
    watch = {"paired": False, "stop": False}
    def watch_pairing():
        time.sleep(3)
        while not watch["stop"] and not watch["paired"]:
            if moonlight_list_status(target)[0] == "paired":
                watch["paired"] = True
                return
            time.sleep(3)
    threading.Thread(target=watch_pairing, daemon=True).start()

    started, cancelled, stick_state = time.time(), False, {}
    while proc.poll() is None and not watch["paired"] and time.time() - started < PAIR_TIMEOUT:
        for event in pygame.event.get():
            if handle_device_event(event):
                continue
            if input_action(event, stick_state) in ("b", "home"):
                cancelled = True
        if cancelled:
            break
        left = int(PAIR_TIMEOUT - (time.time() - started))
        pairing_popup([f"Pair with {name}",
                       # Sunshine's web UI is on its HTTP port + 1 (47990 by default)
                       f"On the PC, open Sunshine's web page (https://localhost:{cfg.get('host.port') + 1}) > PIN,",
                       "and enter this PIN with a name for this Pi:"],
                      pin=pin, footer=f"Waiting for the PIN... {left // 60}:{left % 60:02d}   B: cancel",
                      draw_bg=draw_bg)
        clock.tick(30)
    watch["stop"] = True
    if proc.poll() is None:  # paired (seen by the watcher), cancelled, timed out, or stuck
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    pygame.event.clear()

    paired = watch["paired"]
    if not paired:  # the real test: does `list` work now? (also after B - the PIN may just have gone in)
        pairing_popup([f"Checking {name}..."], draw_bg=draw_bg)
        paired = moonlight_list_status(target)[0] == "paired"
    if cancelled and not paired:
        return False
    if target == MOONLIGHT_HOST:
        host_paired = paired
        if paired:  # show the real app tiles straight away
            pairing_popup([f"Paired with {name} - loading apps..."], draw_bg=draw_bg)
            if refresh_host_apps() is None:
                last_apps_fetch = 0  # didn't work this time - retry on the next status refresh
    confirm_popup("Paired" if paired else "Pairing failed",
                  [f"This Pi is now paired with {name}." if paired else
                   "Check the PIN was entered in Sunshine, then try again."],
                  confirm_label="OK", cancel_label=None, draw_bg=draw_bg)
    return paired

def set_up_host():
    """First run: no host yet. Type it in, save it, and restart the launcher
    so everything picks it up (the host is read at startup)."""
    new = edit_setting_text(cfg.BY_KEY["host.address"], draw_frame)
    if not new:
        return
    cfg.set("host.address", new)
    show_status_popup(f"Host set to {new} - restarting...")
    pygame.time.wait(1500)
    pygame.quit()
    sys.exit(0)  # not 99: the boot loop starts Lunapad again

def require_moonlight():
    """True if moonlight-qt is installed; otherwise explains how to install it
    (Lunapad never installs it itself) and returns False."""
    global moonlight_present
    moonlight_present = shutil.which("moonlight-qt") is not None
    if moonlight_present:
        return True
    confirm_popup("Moonlight isn't installed", [
        "Streaming needs Moonlight Qt 6.2.0 or newer, which isn't installed.",
        "Install it from a terminal: see Installation in Lunapad's README,",
        "or Moonlight's Raspberry Pi guide. No restart needed afterwards.",
    ], confirm_label="OK", cancel_label=None)
    return False

def activate_app(app):
    """A on an app tile. If that app's session is running: resume or quit it.
    If a different app is running: confirm, close it, then launch."""
    if not require_moonlight():
        return
    if host_paired is False:
        if confirm_popup("Not paired", [f"This Pi isn't paired with {host_pc_name or HOST} yet."],
                         confirm_label="Pair now", cancel_label="Cancel"):
            run_pairing()
        return
    running = running_app_name()
    cmd = node_cmd(app_key(app))
    if running == app:
        choice = choice_popup(f"{app} is running", ["Resume", "Quit app", "Cancel"])
        if choice == "Resume":
            launch(cmd)  # streaming the running app resumes its session
        elif choice == "Quit app":
            quit_running_app(app)
        return
    if running:
        if not confirm_popup(f"{running} is running",
                             [f"Launching {app} will close it."],
                             confirm_label="Close & Launch", cancel_label="Cancel"):
            return
        if not quit_running_app(running):
            return
    activate(cmd)

def activate(cmd):
    global selected
    if cmd is None:
        if confirm_popup("Exit picker?",
                          ["This returns you to the console.", "Reboot to relaunch."],
                          confirm_label="Exit", cancel_label="Cancel"):
            pygame.quit()
            sys.exit(99)
    elif cmd == "UPDATE":
        title = f"{pending_updates} update(s) available" if pending_updates > 0 else "No updates currently pending"
        choice = choice_popup(title, ["Update Now", "Show Updates", "Cancel"])
        if choice == "Update Now":
            do_update()
        elif choice == "Show Updates":
            pkgs = get_pending_update_list()
            if pkgs:
                shown = pkgs[:20]
                lines = shown + ([f"...and {len(pkgs) - 20} more"] if len(pkgs) > 20 else [])
            else:
                lines = ["No updates currently pending."]
            if confirm_popup("Pending Updates", lines, confirm_label="Update Now", cancel_label="Cancel"):
                do_update()
    elif cmd == "REBOOT":
        if confirm_popup("Reboot the Pi?", ["This will restart the device now."],
                          confirm_label="Reboot", cancel_label="Cancel"):
            do_reboot()
    elif cmd == "SET_HOST":
        set_up_host()
    elif cmd == "PAIR":
        run_pairing()
    elif cmd == "SETTINGS":
        run_settings_page()
        selected = None  # back on the first app tile, not the Settings chip
    elif cmd == "RELOAD_APPS":
        reload_apps()
    elif cmd == "BLUETOOTH":
        run_bluetooth_page()
    elif isinstance(cmd, list) and "stream" in cmd:
        lines = [
            "To exit the stream, use:",
            "Ctrl+Alt+Shift+Q (keyboard)",
            "or Start+Select+L1+R1 (gamepad)",
        ]
        if confirm_popup("Before you launch", lines):
            launch(cmd)
    elif cmd == ["moonlight-qt"]:
        if not require_moonlight():
            return
        lines = ["Press B to exit"]
        if confirm_popup("Before you launch", lines):
            launch(cmd)
    else:
        launch(cmd)

menu_stick_state = {}

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == CONTROLLER_REFRESH_EVENT:
            refresh_status_background()
        if event.type == UPDATE_CHECK_EVENT:
            last_check_time, _ = load_update_state()
            if not checking_updates and time.time() - last_check_time > CHECK_INTERVAL:
                threading.Thread(target=check_updates_background, daemon=True).start()
        if event.type == IDLE_CHECK_EVENT:
            # once per idle period: only if nothing's been pressed since the last disconnect
            disconnect_after = cfg.get("idle.disconnect_after_minutes") * 60  # 0 = never
            if disconnect_after and time.time() - last_activity > disconnect_after \
                    and last_idle_disconnect < last_activity:
                # bluetoothctl calls can take seconds - keep them off the UI loop
                threading.Thread(target=disconnect_idle_controllers, daemon=True).start()
                last_idle_disconnect = time.time()
        if handle_device_event(event):
            continue
        action = input_action(event, menu_stick_state)
        if action in ("up", "down", "left", "right"):
            selected = nav_move(selected, action)
        elif action == "a":
            if selected.startswith("app:"):
                activate_app(selected[4:])
            else:
                activate(node_cmd(selected))
        elif action == "b" and selected.startswith("app:") and running_app_name() == selected[4:] \
                and moonlight_present:
            # B on the running app's tile quits it - confirmed, since B is
            # "back" everywhere else and a stray press shouldn't end a game
            app = selected[4:]
            if confirm_popup(f"Quit {app}?", ["This ends the session on the host."],
                             confirm_label="Quit", cancel_label="Cancel"):
                quit_running_app(app)

    draw_frame()
    draw_screen_dim()
    pygame.display.flip()
    clock.tick(30)
