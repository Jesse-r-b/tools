#!/usr/bin/env python3
"""node_builder.py - curses console tool to configure an OpenMANET
node (hostname, mesh gate/point role, HaLow mesh settings, AP radios,
battery monitor, cpufreq fix) through a menu, then produce a
ready-to-flash factory image named after the node and optionally write
it straight to an SD card.

This exists so bringing up a multi-node mesh test doesn't require
plugging each device into the network one at a time to run the setup
wizard - see uci_config.py's module docstring for exactly what's
hardware-verified vs code-derived, and image_patcher.py for how the
config actually gets baked in (loop-mount an existing built .img.gz,
inject config + a first-boot script, re-gzip - this tool does not
invoke the OpenWrt build system itself).

Hardware assumption: Raspberry Pi 3 + Wio-WM6108 (Morse Micro MM6108)
SPI HaLow HAT, the only board this project has exercised - radio1 is
the onboard 2.4GHz broadcom chip, radio2 is the SPI HaLow radio. See
RADIO1_PATH / RADIO2_PATH below if targeting different hardware.

Usage:
    sudo ./node_builder.py
"""
import curses
import curses.textpad
import json
import os
import secrets
import sys

import image_patcher
import regdb
import sdcard_write
import uci_config

NODES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodes")
RADIO1_PATH = "platform/soc/3f300000.mmcnr/mmc_host/mmc1/mmc1:0001/mmc1:0001:1"
RADIO2_PATH = "platform/soc/3f204000.spi/spi_master/spi0/spi0.0"

REGDB = regdb.RegDB()


# ---------------------------------------------------------------------------
# Profile defaults
# ---------------------------------------------------------------------------

def _default_ap_ssid(hostname):
    return f"WiFi-{hostname}"


def _random_secret(nbytes=12):
    """A fresh random secret per profile - never a hardcoded literal.
    A shared hardcoded default here previously leaked the real mesh
    passphrase into public git history because nobody ever had a
    reason to change it away from the tool's own default."""
    return secrets.token_urlsafe(nbytes)


def default_profile(hostname="Node-1"):
    country = REGDB.default_country()
    bw = REGDB.narrowest_bandwidth(country)
    channels = REGDB.channels(country, bw)
    channel = channels[len(channels) // 2] if channels else 1

    return {
        "hostname": hostname,
        "admin_password": _random_secret(),
        "role": "gate",
        "gate_mode": "router",
        "point_mode": "extender",
        "uplink_type": "ethernet",
        "uplink_ethernet_port": "eth0",
        "mesh": {
            "radio": "radio2",
            "path": RADIO2_PATH,
            "mesh_id": "jbMesh",
            "passphrase": _random_secret(),
            "encryption": "sae",
            "country": country,
            "bandwidth_mhz": bw,
            "channel": channel,
            "watchdog_interval_secs": 60,
            "spi_clock_speed": 20000000,
            "bcf": "bcf_fgh100mhaamd.bin",
        },
        "mesh_iface_ssid": "",
        "meshap_key": "",
        "aps": [
            {
                "radio": "radio1",
                "path": RADIO1_PATH,
                "band": "2g",
                "channel": "6",
                "htmode": "HT20",
                "enabled": True,
                "ssid": _default_ap_ssid(hostname),
                "passphrase": _random_secret(),
                "encryption": "psk2",
            }
        ],
        "battery": {
            "enabled": False,
            "sensor_type": "ina219",
            "i2c_bus": "/dev/i2c-1",
            "i2c_address": None,
            "sense_resistor_milliohm": 100,
            "max_current_milliamp": 3200,
            "min_voltage": 6.0,
            "max_voltage": 8.4,
        },
        "cpufreq_performance": True,
        "base_image_path": "",
    }


def profile_path(hostname):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in hostname)
    return os.path.join(NODES_DIR, f"{safe}.json")


def save_profile(profile):
    os.makedirs(NODES_DIR, exist_ok=True)
    with open(profile_path(profile["hostname"]), "w") as f:
        json.dump(profile, f, indent=2)


def list_profiles():
    if not os.path.isdir(NODES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(NODES_DIR) if f.endswith(".json"))


def load_profile(hostname):
    with open(profile_path(hostname)) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# curses helpers
# ---------------------------------------------------------------------------

def _c(idx256, basic):
    """256-color palette index when the terminal supports it (nearly all
    modern terminal emulators do), else fall back to a standard 8-color
    curses.COLOR_* constant. Avoids relying on init_color()/dynamic
    palette reprogramming, which not every terminal honors."""
    return idx256 if curses.COLORS >= 256 else basic


def init_colors(stdscr):
    curses.start_color()
    # A muted, low-saturation dark palette (Nord/Gruvbox family) instead
    # of full-bright ANSI blue/cyan blocks - desaturated colors on a
    # near-black (not pure-black) background read as calm instead of
    # "offensive", and every pair shares the same background so the
    # whole screen reads as one solid panel (text drawn without an
    # explicit pair still inherits it via stdscr.bkgd()).
    bg = _c(236, curses.COLOR_BLACK)      # near-black slate, not pure black
    fg = _c(253, curses.COLOR_WHITE)      # soft off-white body text
    accent = _c(116, curses.COLOR_CYAN)   # muted teal, not neon cyan
    ok = _c(108, curses.COLOR_GREEN)      # muted sage green
    warn = _c(222, curses.COLOR_YELLOW)   # muted gold
    crit = _c(167, curses.COLOR_RED)      # muted brick red
    if curses.COLORS >= 256:
        sel_fg, sel_bg = fg, 240          # light text on a lighter grey bar
    else:
        sel_fg, sel_bg = curses.COLOR_BLACK, curses.COLOR_CYAN

    curses.init_pair(1, accent, bg)   # accent / field border
    curses.init_pair(2, warn, bg)     # warn
    curses.init_pair(3, sel_fg, sel_bg)  # selection highlight
    curses.init_pair(4, crit, bg)     # crit / error
    curses.init_pair(5, ok, bg)       # ok / success
    curses.init_pair(6, fg, bg)       # titles / panel fill / body text
    stdscr.bkgd(" ", curses.color_pair(6))


_STYLE_PAIRS = {"ok": 5, "action": 5, "warn": 2, "crit": 4, "accent": 1}
_MESSAGE_TITLES = {2: "NOTICE", 4: "ERROR", 5: "SUCCESS"}


def _safe_addstr(stdscr, y, x, text, attr=curses.A_NORMAL):
    """addstr that clips to the screen instead of raising when text
    would run past the edge (narrow terminal, resize mid-draw, the
    bottom-right cell curses refuses to write to)."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w - 1:
        return
    try:
        stdscr.addstr(y, x, text[: w - 1 - x], attr)
    except curses.error:
        pass


def _style_attr(style):
    if style is None:
        return curses.color_pair(6)
    if style == "muted":
        return curses.color_pair(6) | curses.A_DIM
    pair = _STYLE_PAIRS.get(style)
    return curses.color_pair(pair) if pair else curses.color_pair(6)


def _item_parts(item):
    return item if len(item) == 3 else (item[0], item[1], None)


PANEL_MAX_W = 78
PANEL_MAX_H = 28


def draw_frame(stdscr, title, hint=None):
    """Every screen shares this chrome: a solid-filled background behind
    a fixed-width panel centered in the terminal (not stretched edge to
    edge), bordered, with the title set into the top border and the key
    hint set into the bottom border. Returns the (top, left, bottom,
    right) usable interior rectangle so callers never hand-place content
    against a different layout per screen."""
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    pw = max(20, min(PANEL_MAX_W, w - 4))
    ph = max(8, min(PANEL_MAX_H, h - 2))
    y0 = max(0, (h - ph) // 2)
    x0 = max(0, (w - pw) // 2)
    stdscr.attron(curses.color_pair(6))
    try:
        curses.textpad.rectangle(stdscr, y0, x0, y0 + ph - 1, x0 + pw - 1)
    except curses.error:
        pass
    stdscr.attroff(curses.color_pair(6))
    _safe_addstr(stdscr, y0, x0 + 2, f" {title} ", curses.color_pair(6) | curses.A_BOLD)
    if hint:
        _safe_addstr(stdscr, y0 + ph - 1, x0 + 2, f" {hint} ", curses.color_pair(6) | curses.A_DIM)
    return y0 + 2, x0 + 2, y0 + ph - 3, x0 + pw - 3


SEP = object()  # sentinel value marking a non-selectable divider row


def _next_selectable(items, idx, step):
    """Step idx by +/-1, skipping over SEP rows (Gestalt proximity: a
    divider groups config fields separately from action buttons, and
    should never itself be reachable as a selection)."""
    n = len(items)
    i = idx
    while 0 <= i + step < n:
        i += step
        if items[i][1] is not SEP:
            return i
    return idx


def select_from_list(stdscr, title, items, start_idx=0, subtitle=None, hint=None):
    """items: list of (label, value) or (label, value, style), style in
    {None, "ok"/"action", "warn", "crit", "accent", "muted"}, or a
    divider built with SEP (see _next_selectable). Returns the chosen
    value, or None if the user cancelled with q/Esc. Scrolls when there
    are more items than fit on screen."""
    curses.curs_set(0)
    idx = min(start_idx, len(items) - 1) if items else 0
    if items and items[idx][1] is SEP:
        idx = _next_selectable(items, idx, 1)
    top_scroll = 0
    while True:
        top, left, bottom, right = draw_frame(
            stdscr, title, hint=hint or "↑/↓ move   Enter select   q cancel")
        content_top = top
        if subtitle:
            _safe_addstr(stdscr, content_top, left, subtitle, curses.color_pair(6) | curses.A_DIM)
            content_top += 2
        visible_rows = max(1, bottom - content_top)

        if items:
            if idx < top_scroll:
                top_scroll = idx
            elif idx >= top_scroll + visible_rows:
                top_scroll = idx - visible_rows + 1

        for i in range(top_scroll, min(len(items), top_scroll + visible_rows)):
            label, val, style = _item_parts(items[i])
            row = content_top + (i - top_scroll)
            if val is SEP:
                _safe_addstr(stdscr, row, left, "─" * (right - left), curses.color_pair(6) | curses.A_DIM)
                continue
            selected = i == idx
            attr = (curses.color_pair(3) | curses.A_BOLD) if selected else _style_attr(style)
            marker = "› " if selected else "  "
            _safe_addstr(stdscr, row, left, marker + label, attr)

        if len(items) > visible_rows:
            _safe_addstr(stdscr, top - 2, max(left, right - 10), f"[{idx + 1}/{len(items)}]",
                         curses.color_pair(6))

        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = _next_selectable(items, idx, -1)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = _next_selectable(items, idx, 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return items[idx][1] if items else None
        elif key in (ord("q"), 27):
            return None


def _edit_textbox(win, initial):
    """An in-place editable field: pre-fills `initial` into a Textbox so
    the user edits existing text (backspace/arrow keys) instead of the
    old erase-the-screen-and-retype-from-scratch flow. Enter submits,
    Esc discards the edit and signals cancellation via None."""
    max_w = win.getmaxyx()[1] - 1
    win.erase()
    win.bkgd(" ", curses.color_pair(6))
    win.addstr(0, 0, initial[:max_w])
    win.move(0, min(len(initial), max_w))
    cancelled = {"v": False}

    def validate(ch):
        if ch in (10, 13):        # Enter -> submit
            return 7
        if ch == 27:               # Esc -> cancel
            cancelled["v"] = True
            return 7
        if ch in (curses.KEY_BACKSPACE, 127):
            return 8
        return ch

    curses.curs_set(1)
    box = curses.textpad.Textbox(win, insert_mode=True)
    try:
        box.edit(validate)
    finally:
        curses.curs_set(0)
    return None if cancelled["v"] else box.gather().strip()


def prompt_text(stdscr, prompt, current=""):
    top, left, bottom, right = draw_frame(stdscr, prompt, hint="Enter confirm   Esc cancel")
    _safe_addstr(stdscr, top, left, "Value (edit in place):", curses.color_pair(6) | curses.A_DIM)
    field_y = top + 2
    field_w = max(10, right - left)
    stdscr.attron(curses.color_pair(1))
    curses.textpad.rectangle(stdscr, field_y - 1, left - 1, field_y + 1, left + field_w)
    stdscr.attroff(curses.color_pair(1))
    stdscr.refresh()
    editwin = curses.newwin(1, field_w, field_y, left)
    result = _edit_textbox(editwin, current)
    return current if not result else result


def prompt_int(stdscr, prompt, current):
    val = prompt_text(stdscr, prompt, str(current))
    try:
        return int(val)
    except ValueError:
        return current


def prompt_bool(stdscr, prompt, current):
    choice = select_from_list(stdscr, prompt, [("On", True), ("Off", False)],
                               start_idx=0 if current else 1)
    return current if choice is None else choice


def message(stdscr, lines, color=None):
    top, left, bottom, right = draw_frame(stdscr, _MESSAGE_TITLES.get(color, "INFO"),
                                           hint="Press any key to continue...")
    attr = curses.color_pair(color) if color else curses.color_pair(6)
    for i, line in enumerate(lines):
        _safe_addstr(stdscr, top + i, left, line, attr)
    stdscr.refresh()
    stdscr.getch()


# ---------------------------------------------------------------------------
# Profile editing screens
# ---------------------------------------------------------------------------

GATE_MODES = [("Router (plain, no NAT)", "router"),
              ("Router+Firewall (NAT on wan)", "router_firewall")]
POINT_MODES = [("Extender (bridges local eth/AP into the mesh)", "extender"),
               ("None (headless, relies on a peer mesh-gate for DHCP)", "none")]
ENCRYPTIONS = [("SAE (WPA3, mesh default)", "sae"), ("PSK2 (WPA2)", "psk2"),
               ("PSK (WPA)", "psk"), ("None (open)", "none")]
BATTERY_TYPES = [("Disabled", None),
                  ("INA219 (generic UPS HAT)", "ina219"),
                  ("Waveshare UPS HAT (D)", "waveshare-mcu-d")]


def edit_role(stdscr, profile):
    role = select_from_list(stdscr, "Role", [("Mesh Gate", "gate"), ("Mesh Point", "point")],
                             start_idx=0 if profile["role"] == "gate" else 1)
    if role is None:
        return
    profile["role"] = role

    if role == "gate":
        mode = select_from_list(stdscr, "Gate mode", GATE_MODES,
                                 start_idx=0 if profile["gate_mode"] == "router" else 1)
        if mode:
            profile["gate_mode"] = mode
        uplink = select_from_list(stdscr, "Uplink type",
                                   [("Ethernet", "ethernet"),
                                    ("WiFi STA (NOT IMPLEMENTED - use the network wizard instead)", "wifi_sta")],
                                   start_idx=0)
        if uplink == "wifi_sta":
            message(stdscr, [
                "WiFi-STA uplink isn't implemented in this tool.",
                "Falling back to ethernet - use the real setup wizard",
                "over the network for a WiFi-STA gate.",
            ], color=2)
            uplink = "ethernet"
        if uplink:
            profile["uplink_type"] = uplink
        if profile["uplink_type"] == "ethernet":
            profile["uplink_ethernet_port"] = prompt_text(
                stdscr, "Ethernet uplink port", profile["uplink_ethernet_port"])
    else:
        mode = select_from_list(stdscr, "Point mode", POINT_MODES,
                                 start_idx=0 if profile["point_mode"] == "extender" else 1)
        if mode:
            profile["point_mode"] = mode


def edit_mesh(stdscr, profile):
    mesh = profile["mesh"]
    mesh["mesh_id"] = prompt_text(stdscr, "Mesh ID (must match on every node)", mesh["mesh_id"])
    mesh["passphrase"] = prompt_text(stdscr, "Mesh passphrase (must match on every node)", mesh["passphrase"])
    enc = select_from_list(stdscr, "Mesh encryption", ENCRYPTIONS,
                            start_idx=[e[1] for e in ENCRYPTIONS].index(mesh["encryption"])
                            if mesh["encryption"] in [e[1] for e in ENCRYPTIONS] else 0)
    if enc:
        mesh["encryption"] = enc

    countries = REGDB.countries()
    country_items = [(f"{name} ({code})", code) for code, name in countries]
    start = next((i for i, (_l, c) in enumerate(country_items) if c == mesh["country"]), 0)
    country = select_from_list(stdscr, "Country (regulatory domain)", country_items, start_idx=start)
    if country:
        mesh["country"] = country
        bws = REGDB.bandwidths(country)
        if mesh["bandwidth_mhz"] not in bws:
            mesh["bandwidth_mhz"] = bws[0] if bws else mesh["bandwidth_mhz"]

    bws = REGDB.bandwidths(mesh["country"])
    bw_items = [(f"{b} MHz" + ("  (narrowest - lowest chip load)" if b == bws[0] else ""), b) for b in bws]
    start = bws.index(mesh["bandwidth_mhz"]) if mesh["bandwidth_mhz"] in bws else 0
    bw = select_from_list(stdscr, f"HaLow bandwidth ({mesh['country']})", bw_items, start_idx=start)
    if bw:
        mesh["bandwidth_mhz"] = bw
        chans = REGDB.channels(mesh["country"], bw)
        if mesh["channel"] not in chans:
            mesh["channel"] = chans[len(chans) // 2] if chans else mesh["channel"]

    chans = REGDB.channels(mesh["country"], mesh["bandwidth_mhz"])
    chan_items = [(str(c), c) for c in chans]
    start = chans.index(mesh["channel"]) if mesh["channel"] in chans else 0
    chan = select_from_list(stdscr, f"Channel ({mesh['country']} @ {mesh['bandwidth_mhz']}MHz)",
                             chan_items, start_idx=start)
    if chan:
        mesh["channel"] = chan

    mesh["watchdog_interval_secs"] = prompt_int(
        stdscr, "Watchdog interval (seconds) - 60 mitigates the health-check crash loop",
        mesh["watchdog_interval_secs"])
    mesh["spi_clock_speed"] = prompt_int(
        stdscr, "SPI clock speed (Hz) - 20000000 (20MHz) fixed the original fatal freeze",
        mesh["spi_clock_speed"])


def edit_ap(stdscr, ap):
    ap["enabled"] = prompt_bool(stdscr, f"AP on {ap['radio']} enabled?", ap["enabled"])
    if not ap["enabled"]:
        return
    ap["ssid"] = prompt_text(stdscr, "SSID", ap["ssid"])
    enc = select_from_list(stdscr, "Encryption", ENCRYPTIONS,
                            start_idx=[e[1] for e in ENCRYPTIONS].index(ap["encryption"])
                            if ap["encryption"] in [e[1] for e in ENCRYPTIONS] else 0)
    if enc:
        ap["encryption"] = enc
    if ap["encryption"] != "none":
        ap["passphrase"] = prompt_text(stdscr, "Passphrase", ap["passphrase"])


def edit_battery(stdscr, profile):
    battery = profile["battery"]
    current_type = battery["sensor_type"] if battery["enabled"] else None
    start = next((i for i, (_l, v) in enumerate(BATTERY_TYPES) if v == current_type), 0)
    choice = select_from_list(stdscr, "Battery monitor", BATTERY_TYPES, start_idx=start)
    if choice is None:
        return
    battery["enabled"] = choice is not None
    if battery["enabled"]:
        battery["sensor_type"] = choice
        battery["i2c_bus"] = prompt_text(stdscr, "I2C bus", battery["i2c_bus"])
        addr = prompt_text(stdscr, "I2C address override (blank = protocol default: "
                                    "0x43 for ina219, 0x2d for waveshare-mcu-d)",
                            str(battery["i2c_address"]) if battery["i2c_address"] else "")
        battery["i2c_address"] = addr if addr else None
        if choice == "ina219":
            battery["sense_resistor_milliohm"] = prompt_int(
                stdscr, "Shunt resistor (milliohm)", battery["sense_resistor_milliohm"])
            battery["max_current_milliamp"] = prompt_int(
                stdscr, "Max current calibration (mA)", battery["max_current_milliamp"])


def profile_menu_items(profile):
    mesh = profile["mesh"]
    role_desc = (f"Gate / {profile['gate_mode']} / {profile['uplink_type']}"
                 if profile["role"] == "gate" else f"Point / {profile['point_mode']}")
    battery_desc = "disabled" if not profile["battery"]["enabled"] else profile["battery"]["sensor_type"]
    aps_desc = ", ".join(f"{a['radio']}={'on' if a['enabled'] else 'off'}" for a in profile["aps"])
    scenario_conf = uci_config.scenario_confidence(profile)
    conf_style = {"HIGH": "ok", "MEDIUM": "warn", "LOW": "crit"}.get(scenario_conf)

    def field(label, value):
        return f"{label:<16}{value}"

    return [
        (field("Hostname", profile["hostname"]), "hostname", None),
        (field("Admin password", "*" * len(profile["admin_password"])), "password", None),
        (field("Role", f"{role_desc}  [{scenario_conf}]"), "role", conf_style),
        (field("Mesh", f"id={mesh['mesh_id']} bw={mesh['bandwidth_mhz']}MHz "
                        f"ch={mesh['channel']} country={mesh['country']}"), "mesh", None),
        (field("AP radios", aps_desc), "aps", None),
        (field("Battery", battery_desc), "battery", None),
        (field("CPU governor", "performance (on)" if profile["cpufreq_performance"] else "default (off)"),
         "cpufreq", None),
        (field("Base image", os.path.basename(profile["base_image_path"])
                if profile["base_image_path"] else "(not set)"),
         "base_image", None if profile["base_image_path"] else "warn"),
        (None, SEP, None),
        ("[ Save Profile ]", "save", "accent"),
        ("[ Build Node Image ]", "build", "ok"),
        ("[ Write Last Built Image to SD Card ]", "write_sdcard", "warn"),
        (None, SEP, None),
        ("[ Back to Main Menu ]", "back", "muted"),
    ]


def find_base_images():
    """Best-effort discovery of already-built factory images under the
    firmware repo, newest first, so the operator usually doesn't have
    to type a path by hand."""
    search_root = "/home/jesse/Src/openmanet/firmware/bin/targets/bcm27xx/bcm2710"
    if not os.path.isdir(search_root):
        return []
    hits = []
    for name in os.listdir(search_root):
        if name.endswith("-ext4-factory.img.gz") and "mm6108-spi" in name:
            full = os.path.join(search_root, name)
            hits.append((os.path.getmtime(full), full))
    hits.sort(reverse=True)
    return [path for _mtime, path in hits]


def edit_base_image(stdscr, profile):
    found = find_base_images()
    items = [(f"{os.path.basename(p)}", p) for p in found]
    items.append(("Type a path manually...", "__manual__"))
    choice = select_from_list(stdscr, "Base factory image (already built, this tool does not build firmware)",
                               items)
    if choice is None:
        return
    if choice == "__manual__":
        profile["base_image_path"] = prompt_text(stdscr, "Base image path", profile["base_image_path"])
    else:
        profile["base_image_path"] = choice


def build_image(stdscr, profile):
    if not profile["base_image_path"] or not os.path.isfile(profile["base_image_path"]):
        message(stdscr, ["No valid base image selected.", "Set 'Base image' first."], color=4)
        return None
    if os.geteuid() != 0:
        message(stdscr, ["Building requires root (loop mounts).",
                          "Re-run this tool with sudo."], color=4)
        return None

    config_files = uci_config.generate_all(profile)
    out_dir = os.path.dirname(profile["base_image_path"])
    safe_hostname = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile["hostname"])
    output_path = os.path.join(out_dir, f"openmanet-{safe_hostname}-factory.img.gz")

    def progress(msg):
        top, left, _bottom, _right = draw_frame(
            stdscr, f"Building image for {profile['hostname']}",
            hint="This can take a few seconds...")
        _safe_addstr(stdscr, top, left, msg, curses.color_pair(6))
        stdscr.refresh()

    progress("starting...")

    try:
        image_patcher.bake_image(profile["base_image_path"], output_path, config_files,
                                  profile["admin_password"], progress=progress)
    except Exception as e:
        message(stdscr, [f"Build failed: {e}"], color=4)
        return None

    conf = uci_config.scenario_confidence(profile)
    message(stdscr, [
        f"Built: {output_path}",
        f"Scenario confidence: {conf}",
        "",
        "Use 'Write last built image to SD card' to flash it, or run",
        "flash-sdcard.py against this path directly.",
    ], color=5)
    return output_path


def profile_editor(stdscr, profile):
    last_built = None
    last_action = None
    while True:
        items = profile_menu_items(profile)
        start_idx = next((i for i, (_l, a, _s) in enumerate(items) if a == last_action), 0)
        action = select_from_list(
            stdscr, f"Configuring: {profile['hostname']}", items, start_idx=start_idx,
            subtitle=f"Scenario confidence: {uci_config.scenario_confidence(profile)}",
            hint="↑/↓ move   Enter edit/run   q back (unsaved changes are lost)")
        if action is None or action == "back":
            return
        last_action = action

        if action == "hostname":
            old_name = profile["hostname"]
            new_name = prompt_text(stdscr, "Hostname", old_name)
            profile["hostname"] = new_name
            if new_name != old_name:
                stale_ssid = _default_ap_ssid(old_name)
                for ap in profile["aps"]:
                    if ap.get("ssid") == stale_ssid:
                        ap["ssid"] = _default_ap_ssid(new_name)
        elif action == "password":
            profile["admin_password"] = prompt_text(stdscr, "Admin password", profile["admin_password"])
        elif action == "role":
            edit_role(stdscr, profile)
        elif action == "mesh":
            edit_mesh(stdscr, profile)
        elif action == "aps":
            for ap in profile["aps"]:
                edit_ap(stdscr, ap)
        elif action == "battery":
            edit_battery(stdscr, profile)
        elif action == "cpufreq":
            profile["cpufreq_performance"] = prompt_bool(
                stdscr, "cpufreq performance governor (recommended: on - see task history)",
                profile["cpufreq_performance"])
        elif action == "base_image":
            edit_base_image(stdscr, profile)
        elif action == "save":
            save_profile(profile)
            message(stdscr, [f"Saved: {profile_path(profile['hostname'])}"], color=5)
        elif action == "build":
            built = build_image(stdscr, profile)
            if built:
                last_built = built
        elif action == "write_sdcard":
            if not last_built:
                message(stdscr, ["Build an image first (this session hasn't built one yet)."], color=4)
                continue
            curses.endwin()
            try:
                device = sdcard_write.write_image_to_sdcard(last_built)
            except Exception as e:
                print(f"Write failed: {e}")
                input("Press Enter to return to the menu...")
            else:
                if device:
                    print(f"Wrote to {device}.")
                input("Press Enter to return to the menu...")
            stdscr = curses.initscr()
            curses.noecho()
            curses.cbreak()
            stdscr.keypad(True)
            init_colors(stdscr)


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu(stdscr):
    init_colors(stdscr)
    curses.curs_set(0)
    while True:
        profiles = list_profiles()
        items = [("[ + New Node Profile ]", "new", "ok")]
        if profiles:
            items.append((None, SEP, None))
            items += [(f"Edit: {p}", ("edit", p), None) for p in profiles]
        items.append((None, SEP, None))
        items.append(("[ Quit ]", "quit", "muted"))

        choice = select_from_list(stdscr, "OpenMANET Node Builder", items,
                                   subtitle="Configure a node, build its factory image, flash it to an SD card.")
        if choice is None or choice == "quit":
            return

        if choice == "new":
            hostname = prompt_text(stdscr, "New node hostname", "Node-1")
            if not hostname:
                continue
            profile = default_profile(hostname)
            profile_editor(stdscr, profile)
        elif isinstance(choice, tuple) and choice[0] == "edit":
            profile = load_profile(choice[1])
            profile_editor(stdscr, profile)


def main():
    if os.geteuid() != 0:
        print("Note: building images and writing SD cards both need root.")
        print("Profile editing alone works without it, but re-run with sudo")
        print("once you're ready to build/write.")
        print()
    os.makedirs(NODES_DIR, exist_ok=True)
    curses.wrapper(main_menu)


if __name__ == "__main__":
    main()
