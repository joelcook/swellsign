# MatrixPortal S3 thin display

This client is intentionally dumb: it accepts only `schema_version: 1`,
`mode: observed` responses from the Swell Sign `/display` endpoint. It never
fetches forecasts or NOAA directly.

## Device setup

1. Install stable CircuitPython 10.2.1 on an Adafruit MatrixPortal S3.
2. Copy `code.py` and a credentialed copy of `settings.example.toml` to
   `CIRCUITPY/`; rename the settings file to `settings.toml`.
3. From the matching CircuitPython 10.x MPY bundle, copy:
   `adafruit_connection_manager.mpy`, `adafruit_requests.mpy`, and the
   `adafruit_display_text/` directory into `CIRCUITPY/lib/`.
4. Put the configured API on the same trusted network and reset the board.

The two horizontal 64×32 panels are one `width=128`, `height=32`, `tile=1`
canvas. Keep both panels upright. Connect the MatrixPortal ribbon to the
right-hand panel `IN`, then that panel's `OUT` to the left-hand panel `IN`.
No address-E jumper is needed for 32-pixel-high panels.

## Power

Design for the two P4 panels' published worst case: 5 V at 8 A total. Use a
regulated 5 V / 10 A supply, short appropriately sized low-voltage wiring, and
a fused split to the panels. Power the MatrixPortal separately through USB-C
and share ground. Do not feed external panel power into the MatrixPortal screw
terminals while USB is connected; those terminals are outputs.

Indoor dimming is done by scaling RGB values with
`SWELLSIGN_BRIGHTNESS`. CircuitPython RGBMatrix treats nonzero
`display.brightness` values as fully enabled, so that property is not used for
night dimming. Start around `0.25`–`0.35` behind smoked diffusion acrylic.

The last valid JSON payload is cached in microcontroller NVM when it fits. The
sign increases its age with monotonic time during a network outage and shows
`OFF`; after a power loss it cannot know the elapsed outage duration until a
new response arrives.
