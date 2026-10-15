"""Reports whether each connected pad is recognised by SDL's GameController
database - the same mapping Moonlight uses, and Lunapad's menu input.
Safe to run while Lunapad is up (no display needed). From Git Bash:
    ssh user@pi python3 - < deploy/check_controllers.py
"""
import os, sys
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame

print("pygame", pygame.version.ver, "- SDL", ".".join(map(str, pygame.get_sdl_version())))
try:
    from pygame._sdl2 import controller
except Exception as e:
    print("pygame._sdl2.controller NOT available:", e)
    sys.exit(1)

pygame.init()
controller.init()
count = pygame.joystick.get_count()
if not count:
    print("No joysticks found - wake/connect the controllers and re-run")
for i in range(count):
    j = pygame.joystick.Joystick(i)
    ok = controller.is_controller(i)
    print(f"\n[{i}] {j.get_name()!r}  guid={j.get_guid()}")
    print(f"    raw: {j.get_numbuttons()} buttons, {j.get_numaxes()} axes, {j.get_numhats()} hats")
    print(f"    SDL game controller: {'YES' if ok else 'NO - needs an SDL_GAMECONTROLLERCONFIG line'}")
    if ok:
        c = controller.Controller(i)
        print(f"    name: {c.name!r}")
        print(f"    mapping: {c.get_mapping()}")
