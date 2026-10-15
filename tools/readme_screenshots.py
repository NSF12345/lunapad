"""Renders the README screenshots (docs/images/*.png, 1280x720) from lunapad.py's
own drawing code, with sample data - no real hosts, addresses or devices.

    pip install pygame-ce
    python tools/readme_screenshots.py [out_dir]     (default: docs/images/)

Re-run it after changing how a screen looks. Like tools/preview_dashboard.py, it
runs lunapad.py up to the main loop with the display swapped for a Surface.
"""
import os, sys, time, tempfile
os.environ["SDL_VIDEODRIVER"] = "dummy"
_t = tempfile.mkdtemp()
os.environ["LUNAPAD_CONFIG_DIR"] = _t + "/c"
os.environ["LUNAPAD_CACHE_DIR"] = _t + "/k"
os.makedirs(_t + "/c")
with open(_t + "/c/lunapad.toml", "w") as f:
    f.write('[host]\naddress = "gaming-pc.lan"\n')
import pygame

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "docs", "images")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, REPO)
real = pygame.display.set_mode
pygame.display.set_mode = lambda *a, **k: (real((1, 1)), pygame.Surface((1920, 1080)))[1]
src = open(REPO + "/lunapad.py", encoding="utf-8").read().split("menu_stick_state = {}")[0]
ns = {"__name__": "x", "__file__": REPO + "/lunapad.py"}
exec(compile(src, "lunapad.py", "exec"), ns)
while ns["_status_refreshing"]:
    time.sleep(0.05)

ns["HOST_DISPLAY"] = "gaming-pc.lan (192.168.1.20)"
cfg = ns["cfg"]
cfg.HOME, cfg.CONFIG_FILE = "/home/lunapad", "/home/lunapad/.config/lunapad/lunapad.toml"  # as on the Pi
ns.update(CLIENT_IP="192.168.1.50", host_reachable=True, pending_updates=3, moonlight_present=True,
          host_paired=True, lunapad_update=None)
ns["host_apps"] = ["Desktop", "Steam Big Picture", "Playnite"]
ns["host_app_ids"] = {"Desktop": 1, "Steam Big Picture": 2, "Playnite": 3}
ns["host_running_app_id"] = 2
pads = [{"name": "Google Stadia Controller", "battery": "85", "connection": "Bluetooth", "mac": None},
        {"name": "8BitDo Ultimate 2C Wireless Controller", "battery": None, "connection": "2.4G", "mac": None}]
cards = ns["label_controllers"]([dict(p, iid=i) for i, p in enumerate(pads)])
ns["build_controller_cards"] = lambda: cards
ns["last_input_at"][0] = time.time()  # the input indicator on the Stadia card

def save(name):
    pygame.image.save(pygame.transform.smoothscale(ns["screen"], (1280, 720)), f"{OUT}/{name}.png")
    print("saved", name)

def first_frame(name, fn):
    """Runs a looping screen, saves its first frame, then presses B to leave."""
    frames = []
    def flip():
        frames.append(1)
        if len(frames) == 1:
            save(name)
            pygame.event.post(pygame.event.Event(pygame.CONTROLLERBUTTONDOWN,
                                                 button=pygame.CONTROLLER_BUTTON_B, instance_id=0))
        if len(frames) > 200:
            raise RuntimeError(f"{name}: never left")
    pygame.display.flip = flip
    fn()

# dashboard
ns["selected"] = None
ns["draw_frame"]()
save("dashboard")

# update popup, with a Lunapad release available
ns["lunapad_update"] = {"label": "0.8.0", "channel": "stable"}
first_frame("update", lambda: ns["activate"]("UPDATE"))
ns["lunapad_update"] = None

# settings page
rows = ns["settings_rows"]()
sel = [i for i, (k, _) in enumerate(rows) if k == "setting"][2]
ns["draw_settings_page"](rows, sel, 0)
save("settings")

# host keyboard (full keyboard on a hostname)
first_frame("keyboard", lambda: ns["edit_setting_text"](cfg.BY_KEY["host.address"], lambda: ns["draw_settings_page"](rows, sel, 0)))

# Bluetooth page
ns["bt_list_known_devices"] = lambda: [
    {"mac": "00:1A:7D:00:00:01", "name": "Stadia Controller", "paired": True, "connected": True, "trusted": True, "battery": "85"},
    {"mac": "00:1A:7D:00:00:02", "name": "Xbox Wireless Controller", "paired": True, "connected": True, "trusted": True, "battery": "60"},
    {"mac": "00:1A:7D:00:00:03", "name": "8BitDo N30 Pro 2", "paired": True, "connected": False, "trusted": True, "battery": None},
    {"mac": "00:1A:7D:00:00:04", "name": "DualSense Wireless Controller", "paired": False, "connected": False, "trusted": False, "battery": None},
]
first_frame("bluetooth", ns["run_bluetooth_page"])
