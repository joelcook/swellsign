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
# These are perceptual levels, not duty cycle: gamma below converts them.
# 0.28 perceptual is roughly 6% duty, which stays visible without lighting
# the room. Validate against the real panel batch before trusting them.
DAY_BRIGHTNESS = float(os.getenv("SWELLSIGN_DAY_BRIGHTNESS", "0.55"))
EVENING_BRIGHTNESS = float(os.getenv("SWELLSIGN_EVENING_BRIGHTNESS", "0.40"))
NIGHT_BRIGHTNESS = float(os.getenv("SWELLSIGN_NIGHT_BRIGHTNESS", "0.28"))
# The board has no configured RTC, so local hour is derived from the payload's
# UTC timestamp plus a fixed offset. New Smyrna is UTC-4 during EDT.
UTC_OFFSET_HOURS = int(os.getenv("SWELLSIGN_UTC_OFFSET_HOURS", "-4"))
GAMMA = float(os.getenv("SWELLSIGN_GAMMA", "2.2"))

# The RGBMatrix brightness property does not dim proportionally above zero, so
# dimming is done by scaling RGB values. The panel applies no perceptual curve
# of its own, so gamma is applied here exactly as the Pi output adapter does.
BASE_COLORS = {
    "cyan": (35, 142, 135),
    "warm": (161, 146, 116),
    "amber": (165, 93, 34),
    "warning": (184, 104, 30),
    "red": (142, 35, 29),
}
BLACK = 0x000000


def channel(value, level):
    """Scale one channel, never letting a lit channel round away to zero.

    Dropping a channel does not dim a color, it changes it: sea glass would
    drift toward pure green as the sign got darker.
    """
    if value <= 0:
        return 0
    return max(1, round(value * level))


def color(red, green, blue, level):
    level = min(1.0, max(0.0, level)) ** GAMMA
    return (
        (channel(red, level) << 16)
        | (channel(green, level) << 8)
        | channel(blue, level)
    )


def palette_for(level):
    return {name: color(*channels, level) for name, channels in BASE_COLORS.items()}


def scheduled_level(payload):
    """Day/evening/night level from the payload's own UTC timestamp."""
    stamp = payload.get("generated_at") if isinstance(payload, dict) else None
    if not isinstance(stamp, str) or len(stamp) < 13:
        return DAY_BRIGHTNESS
    try:
        hour = (int(stamp[11:13]) + UTC_OFFSET_HOURS) % 24
    except ValueError:
        return DAY_BRIGHTNESS
    if 7 <= hour < 19:
        return DAY_BRIGHTNESS
    if 19 <= hour < 22:
        return EVENING_BRIGHTNESS
    return NIGHT_BRIGHTNESS


COLORS = palette_for(BRIGHTNESS)
CYAN = COLORS["cyan"]
WARM = COLORS["warm"]
AMBER = COLORS["amber"]
WARNING = COLORS["warning"]
RED = COLORS["red"]

displayio.release_displays()
matrix = rgbmatrix.RGBMatrix(
    width=128,
    height=32,
    # A dim, gamma-corrected face lives in the bottom of the range, where four
    # bits (16 levels) posterize badly. Six bits buys 64 levels at some refresh
    # cost; drop back to 4 if the panel flickers on the real batch.
    bit_depth=int(os.getenv("SWELLSIGN_BIT_DEPTH", "6")),
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
    colors = palette_for(scheduled_level(payload))
    header.text = payload.get("spot", "")[:16]
    header.color = colors["warm"]
    age = age_text(payload, elapsed_seconds)
    state = payload.get("data_state", "unavailable")
    if offline:
        status.text = "OFF " + age
        status.color = colors["warning"]
    elif payload.get("fallback_used"):
        status.text = "ALT " + age
        status.color = colors["warning"]
    elif state == "stale":
        status.text = "STL " + age
        status.color = colors["warning"]
    elif state == "delayed":
        status.text = "DLY " + age
        status.color = colors["warning"]
    else:
        status.text = age
        status.color = colors["warm"]

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
        wave.color = colors["cyan"]
    else:
        wave.text = "      NO WAVE DATA"
        wave.color = colors["red"]

    wind_data = payload.get("wind")
    if wind_data:
        speed = wind_data.get("speed_mph")
        speed_text = "--" if speed is None else str(round(float(speed)))
        wind.text = "WIND {}             {}MPH".format(
            wind_data.get("direction") or "--", speed_text
        )
    else:
        wind.text = "WIND --"
    wind.color = colors["amber"]
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
