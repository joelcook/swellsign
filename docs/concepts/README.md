# Shop display concepts

Three directions for a Frame TV in a surf shop or coffee shop, as distinct from
the sign in a living room. Open `index.html` in a browser, or:

```bash
cd docs/concepts && python3 -m http.server 8200
```

Data is real: five Florida spots pulled live from NDBC `latest_obs` at the time
of capture, verified against each buoy's own realtime feed.

**Departure board.** The right metaphor for a shop. Communal information you
scan and act on, and a departure board never tells you whether your flight is
*good* — the same posture as the sign. Reads across a room, holds a region.

**Editorial.** Décor first. The owner hangs it because the room looks better,
and it happens to answer the question. One spot, large. Implemented for real in
`src/swellsign/display/editorial.py`.

**Board over photo.** Density plus warmth. Direction is dropped because six
columns will not fit a narrower board; the swell line already implies it.

The recommendation is the board and the editorial **alternating** rather than
either alone: the photo earns the wall as décor, the board earns it as
information, and neither has to be both. It also handles a flat day, where the
board stays useful and the photo stays pretty.

Palette carries from the sign deliberately — sea glass, warm white, amber — so
the shop display and the physical instrument read as one product.
