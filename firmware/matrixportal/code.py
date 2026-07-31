"""Swell Sign thin client for Adafruit MatrixPortal S3 / CircuitPython 10.x."""

import json
import os
import time

import adafruit_connection_manager
import adafruit_requests
import board
import displayio
import framebufferio
import microcontroller
import rgbmatrix
import terminalio
import wifi
from adafruit_display_text import label

WIDTH = 128
HEIGHT = 32
CACHE_MARKER = b"SWL1"
API_URL = os.getenv("SWELLSIGN_API_URL")
REFRESH_SECONDS = int(os.getenv("SWELLSIGN_REFRESH_SECONDS", "15"))
BRIGHTNESS = float(os.getenv("SWELLSIGN_BRIGHTNESS", "0.35"))


def color(red, green, blue):
    level = min(1.0, max(0.0, BRIGHTNESS))
    return (int(red * level) << 16) | (int(green * level) << 8) | int(blue * level)


BLACK = 0x000000
CYAN = color(35, 142, 135)
WARM = color(161, 146, 116)
AMBER = color(165, 93, 34)
WARNING = color(184, 104, 30)
RED = color(142, 35, 29)

displayio.release_displays()
matrix = rgbmatrix.RGBMatrix(
    width=128,
    height=32,
    bit_depth=4,
    addr_pins=board.MTX_ADDRESS[:4],
    tile=1,
    serpentine=False,
    doublebuffer=True,
    **board.MTX_COMMON,
)
display = framebufferio.FramebufferDisplay(
    matrix,
    auto_refresh=False,
    rotation=0,
)
root = displayio.Group()
display.root_group = root

header = label.Label(terminalio.FONT, text="", color=WARM, x=0, y=4)
status = label.Label(terminalio.FONT, text="", color=WARM, anchor_point=(1.0, 0.0), x=127, y=0)
wave = label.Label(terminalio.FONT, text="", color=CYAN, x=0, y=14)
wind = label.Label(terminalio.FONT, text="", color=AMBER, x=0, y=25)
root.append(header)
root.append(status)
root.append(wave)
root.append(wind)


def save_cache(payload):
    try:
        body = json.dumps(payload).encode("utf-8")
        available = len(microcontroller.nvm) - 6
        if len(body) > available:
            return
        microcontroller.nvm[0:4] = CACHE_MARKER
        microcontroller.nvm[4] = (len(body) >> 8) & 0xFF
        microcontroller.nvm[5] = len(body) & 0xFF
        microcontroller.nvm[6 : 6 + len(body)] = body
    except (OSError, ValueError):
        pass


def load_cache():
    try:
        if bytes(microcontroller.nvm[0:4]) != CACHE_MARKER:
            return None
        length = (microcontroller.nvm[4] << 8) | microcontroller.nvm[5]
        if length <= 0 or length > len(microcontroller.nvm) - 6:
            return None
        return json.loads(bytes(microcontroller.nvm[6 : 6 + length]).decode("utf-8"))
    except (OSError, ValueError):
        return None


def valid_payload(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != 1 or payload.get("mode") != "observed":
        return False
    return isinstance(payload.get("spot"), str) and bool(payload.get("generated_at"))


def age_text(payload, elapsed_seconds):
    wave_data = payload.get("wave")
    if not wave_data:
        return "--M"
    base_age = int(wave_data.get("age_minutes", 0))
    return f"{min(999, base_age + int(elapsed_seconds // 60))}M"


def format_decimal(value):
    if value is None:
        return "--"
    return f"{float(value):.1f}"


def render(payload, offline, elapsed_seconds):
    header.text = payload.get("spot", "")[:16]
    age = age_text(payload, elapsed_seconds)
    state = payload.get("data_state", "unavailable")
    if offline:
        status.text = "OFF " + age
        status.color = WARNING
    elif payload.get("fallback_used"):
        status.text = "ALT " + age
        status.color = WARNING
    elif state == "stale":
        status.text = "STL " + age
        status.color = WARNING
    elif state == "delayed":
        status.text = "DLY " + age
        status.color = WARNING
    else:
        status.text = age
        status.color = WARM

    wave_data = payload.get("wave")
    if wave_data:
        trend = {"rising": "^", "falling": "v", "steady": "-"}.get(
            wave_data.get("trend"), " "
        )
        wave.text = "{} {}FT {} {}S {}".format(
            wave_data.get("label", "SEAS")[:5],
            format_decimal(wave_data.get("height_ft")),
            trend,
            format_decimal(wave_data.get("period_s")),
            wave_data.get("direction") or "--",
        )
        wave.color = CYAN
    else:
        wave.text = "      NO WAVE DATA"
        wave.color = RED

    wind_data = payload.get("wind")
    if wind_data:
        speed = wind_data.get("speed_mph")
        speed_text = "--" if speed is None else str(round(float(speed)))
        wind.text = "WIND {}             {}MPH".format(
            wind_data.get("direction") or "--", speed_text
        )
    else:
        wind.text = "WIND --"
    display.refresh()


def connect():
    while not wifi.radio.connected:
        try:
            wifi.radio.connect(
                os.getenv("CIRCUITPY_WIFI_SSID"),
                os.getenv("CIRCUITPY_WIFI_PASSWORD"),
            )
        except OSError:
            time.sleep(3)


connect()
pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
requests = adafruit_requests.Session(pool, ssl_context)

payload = load_cache()
last_good = time.monotonic()
if payload and valid_payload(payload):
    render(payload, True, 0)

failures = 0
while True:
    try:
        if not wifi.radio.connected:
            connect()
        with requests.get(
            API_URL,
            headers={"Accept": "application/json"},
            timeout=10,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            candidate = response.json()
        if not valid_payload(candidate):
            raise ValueError("incompatible display payload")
        payload = candidate
        save_cache(payload)
        last_good = time.monotonic()
        failures = 0
        render(payload, False, 0)
    except (OSError, RuntimeError, ValueError, adafruit_requests.OutOfRetries):
        failures += 1
        if payload:
            render(payload, True, time.monotonic() - last_good)
        if failures >= 3:
            adafruit_connection_manager.connection_manager_close_all(pool)
            failures = 0
    time.sleep(REFRESH_SECONDS)
