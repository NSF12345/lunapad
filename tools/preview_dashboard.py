"""Renders Lunapad's dashboard off-screen at 1920x1080 with fake controller
data - for checking layout changes on a PC before deploying them.

    pip install pygame-ce          # or pygame; pygame-ce has wheels for newer Pythons
    python tools/preview_dashboard.py [out_dir]     (default: preview_out/)

Writes one dashboard_<scenario>.png per controller scenario plus left_panels.png
(the controllers panel of each, side by side), and apps_<n>.png for host app
lists of different lengths.

It runs lunapad.py's own code up to the main loop, with the display swapped for
a plain Surface (SDL's dummy driver alone ignores the requested resolution),
settings and cache in a temporary folder, and the Pi-only commands
(bluetoothctl, apt, ...) simply failing as they would with nothing connected.
Text may differ slightly from the Pi if the local pygame's default font does.
"""
import os, sys, time, tempfile
os.environ["SDL_VIDEODRIVER"] = "dummy"
_tmp = tempfile.mkdtemp(prefix="lunapad-preview-")  # never touch the real ~/.config / ~/.cache
os.environ["LUNAPAD_CONFIG_DIR"] = os.path.join(_tmp, "config")
os.environ["LUNAPAD_CACHE_DIR"] = os.path.join(_tmp, "cache")
os.makedirs(os.environ["LUNAPAD_CONFIG_DIR"])
with open(os.path.join(os.environ["LUNAPAD_CONFIG_DIR"], "lunapad.toml"), "w") as f:
    f.write('[host]\naddress = "gaming-pc.lan"\n')  # an example host (a fresh install has none)
import pygame

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "preview_out"))
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, REPO)

_real_set_mode = pygame.display.set_mode
def fake_set_mode(size=(0, 0), flags=0, *a, **k):
    _real_set_mode((1, 1))  # a real mode must exist for convert_alpha()
    return pygame.Surface((1920, 1080))
pygame.display.set_mode = fake_set_mode

src = open(os.path.join(REPO, "lunapad.py"), encoding="utf-8").read()
src = src.split("menu_stick_state = {}")[0]  # everything before the main loop
ns = {"__name__": "lunapad_preview", "__file__": os.path.join(REPO, "lunapad.py")}
exec(compile(src, "lunapad.py", "exec"), ns)

while ns["_status_refreshing"]:  # let the startup refresh finish before faking data
    time.sleep(0.05)
ns["CLIENT_IP"] = "192.168.1.50"
ns["host_reachable"] = True
ns["pending_updates"] = 3

stadia = {"name": "Google Stadia Controller", "battery": "85", "connection": "Bluetooth", "mac": None}
bitdo = {"name": "8BitDo N30 Pro 2", "battery": None, "connection": "Bluetooth", "mac": None}
usb = {"name": "Xbox Wireless Controller", "battery": None, "connection": "USB", "mac": None}
dongle = {"name": "8BitDo Ultimate 2C Wireless Controller", "battery": None, "connection": "2.4G", "mac": None}
long_name = {"name": "Some Very Long Third-Party Wireless Gamepad Name", "battery": "15",
             "connection": "Bluetooth", "mac": None}
scenarios = {
    "0_none": [],
    "1_one": [bitdo],
    "2_two": [stadia, bitdo],
    "3_mixed": [stadia, dongle, dict(dongle), usb],  # two identical 2Cs -> #1, #2
    "4_four": [stadia, bitdo, usb, long_name],
}

shots = []
for name, ctrls in scenarios.items():
    cards = ns["label_controllers"]([dict(c, iid=i) for i, c in enumerate(ctrls)])
    ns["build_controller_cards"] = lambda cards=cards: cards
    ns["last_input_at"].clear()
    if name == "3_mixed":  # show the input indicator on 2C #1
        ns["last_input_at"][1] = time.time()
    ns["draw_frame"]()
    path = os.path.join(OUT, f"dashboard_{name}.png")
    pygame.image.save(ns["screen"], path)
    shots.append(ns["screen"].copy())
    print("saved", path)

crop = pygame.Rect(40, 130, 600, 740)  # the controllers panel
sheet = pygame.Surface((crop.w * len(shots) + 20 * (len(shots) - 1), crop.h))
sheet.fill((60, 60, 60))
for i, shot in enumerate(shots):
    sheet.blit(shot, (i * (crop.w + 20), 0), crop)
path = os.path.join(OUT, "left_panels.png")
pygame.image.save(sheet, path)
print("saved", path)

# Host app lists of different lengths (the tile grid is built from these).
two = ns["label_controllers"]([dict(stadia, iid=0), dict(bitdo, iid=1)])
ns["build_controller_cards"] = lambda: two
ns["last_input_at"].clear()
app_lists = {
    "2": ["Desktop", "Steam Big Picture"],
    "3": ["Desktop", "Steam Big Picture", "Playnite"],
    "6": ["Desktop", "Steam Big Picture", "Playnite", "Xbox App",
          "A Game With An Unreasonably Long Title: Deluxe Edition", "Emulation"],
}
for name, apps in app_lists.items():
    ns["host_apps"] = apps
    ns["selected"] = None
    ns["draw_frame"]()
    path = os.path.join(OUT, f"apps_{name}.png")
    pygame.image.save(ns["screen"], path)
    print("saved", path)

# A session running on the host (Steam Big Picture): host line + its tile,
# once with another tile selected and once with the running tile selected.
ns["host_apps"] = ["Desktop", "Steam Big Picture"]
ns["host_app_ids"] = {"Desktop": 1, "Steam Big Picture": 2}
ns["host_running_app_id"] = 2
for name, sel in (("other_selected", "app:Desktop"), ("running_selected", "app:Steam Big Picture")):
    ns["selected"] = sel
    ns["draw_frame"]()
    path = os.path.join(OUT, f"session_{name}.png")
    pygame.image.save(ns["screen"], path)
    print("saved", path)
ns["host_running_app_id"] = 0
