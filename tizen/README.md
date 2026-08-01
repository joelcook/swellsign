# Swell Sign on a Samsung TV

Two independent paths, because Tizen apps and Art Mode are mutually exclusive
states of the television.

| | Tizen app (this folder) | Art Mode (`swellsign frame-tv`) |
|---|---|---|
| Visible when | TV is **on**, app launched | TV is **off**, in standby |
| Runs code on the TV | Yes, a web app | No — it displays an uploaded image |
| Feels like | a destination you open | ambient, always there |

Both consume the same server-rendered `frame.png`, so neither can drift from
what the physical sign shows.

## The app

Roughly a hundred lines, and deliberately dumb: it fetches a rendered image and
displays it. All layout lives in the Python renderer. A second implementation of
the face in JavaScript would be a second source of truth about what the sign
says, which is the thing the whole project is organised to avoid.

Edit `API` and `SPOT` at the top of the script block in `index.html` before
building. Over a Cloudflare Tunnel that is your public hostname; on the LAN it
is whatever runs `swellsign api`.

## Sideloading to your own TV

You do not need to publish anything. Store submission only matters if other
people are going to install it, and it brings a seller account, an age rating,
and a 7–20 business day review with it.

1. Install [Tizen Studio](https://developer.samsung.com/smarttv/develop) with
   the TV extensions.
2. On the TV: **Apps → press `1 2 3 4 5` on the remote → Developer Mode on →
   enter your computer's IP.** Restart the TV.
3. In Tizen Studio, create a certificate profile. The Samsung certificate step
   needs a Samsung account and is confirmed by hand, so allow time.
4. Connect to the TV by IP in Device Manager, then **Run As → Tizen Web
   Application**.

The app appears in the TV's app list and survives reboots. Re-running from
Tizen Studio replaces it.

## Art Mode instead

`swellsign` is not published to PyPI, so install the dependency directly and
run from the source tree the way everything else here does:

```bash
pip3 install samsungtvws
PYTHONPATH=src python3 -m swellsign frame-tv --host 192.168.1.50 --once
PYTHONPATH=src python3 -m swellsign frame-tv --host 192.168.1.50
```

The first connection makes the TV show an "allow this device?" prompt. Accept
it; the token is cached in `data/frame-tv-token.txt` so it is asked once.

The uploader deletes its own previous uploads and keeps a rolling window of two,
because the TV stores a finite number of images and pushing every fifteen
minutes would otherwise fill it within days. It never touches images it did not
upload, so Art Store purchases and personal photos are left alone.

## Untested against hardware

Neither path has run against a real Frame. The Samsung WebSocket API is
unofficial, so calls are best-effort and failures are logged rather than raised.
Expect to adjust. The likely rough edges are the pairing handshake, whether
`matte="none"` renders as intended, and how gracefully Art Mode handles being
handed a new image while displaying the previous one.
