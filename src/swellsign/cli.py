"""Command-line operations for the backend, simulator, and physical sign."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import signal
import time
import webbrowser
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .config import get_product_config, get_settings
from .display.client import DisplayClient, recalculate_ages
from .display.hub75 import PiMatrixOutput
from .display.palette import BrightnessController, BrightnessSchedule
from .display.renderer import DisplayRenderer, animation_phase
from .display.simulator import render_json_file
from .services.collector import build_default_collection_service
from .services.snapshot import SnapshotComposer, compact_display_payload
from .services.tide import TideContextService
from .storage import SQLiteRepository

app = typer.Typer(
    no_args_is_help=True,
    help="Observe, archive, serve, and display the ocean without scoring it.",
)


def _runtime() -> tuple[object, object, SQLiteRepository]:
    settings = get_settings()
    product_config = get_product_config()
    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    repository.upsert_stations(product_config.stations.values())
    return settings, product_config, repository


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class CollectorAlreadyRunning(RuntimeError):
    """Raised when another collector owns this database's process lock."""


class _CollectorLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> _CollectorLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.handle.close()
            self.handle = None
            raise CollectorAlreadyRunning(
                f"another collector already owns {self.path}"
            ) from error
        return self

    def __exit__(self, *_args: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


@app.command("init-db")
def init_db() -> None:
    """Create/upgrade SQLite and synchronize configured stations."""
    settings, product_config, repository = _runtime()
    typer.echo(
        json.dumps(
            {
                "database": str(settings.database_path),
                "schema_ready": repository.is_ready(),
                "stations": len(product_config.stations),
                "counts": repository.counts(),
            },
            indent=2,
        )
    )


@app.command("collect-once")
def collect_once(
    forecast: Annotated[
        bool,
        typer.Option("--forecast/--no-forecast", help="Also archive a seven-day forecast run."),
    ] = False,
    tide: Annotated[
        bool,
        typer.Option("--tide/--no-tide", help="Also archive CO-OPS high/low predictions."),
    ] = False,
) -> None:
    """Fetch current observation sources once, with optional forecast and tide."""
    settings, product_config, repository = _runtime()
    _configure_logging(settings.log_level)
    service = build_default_collection_service(settings, product_config, repository)

    async def run_once() -> list[dict[str, object]]:
        results = [asdict(await service.collect_observations_once())]
        if forecast:
            results.append(asdict(await service.collect_forecast_once()))
        if tide:
            results.append(asdict(await service.collect_tide_once()))
        return results

    typer.echo(json.dumps(asyncio.run(run_once()), default=str, indent=2))


@app.command("tide")
def tide_context(
    spot_id: Annotated[str, typer.Argument(help="Configured spot identifier.")] = "new-smyrna",
) -> None:
    """Print the derived tide phase. Always a prediction, never a measurement."""
    _, product_config, repository = _runtime()
    phase = TideContextService(repository, product_config).phase(spot_id)
    if phase is None:
        typer.echo(
            json.dumps(
                {
                    "mode": "prediction",
                    "spot_id": spot_id,
                    "phase": None,
                    "detail": "no pair of predicted extremes brackets this moment",
                },
                indent=2,
            )
        )
        return
    typer.echo(phase.model_dump_json(indent=2))


@app.command("lag")
def publication_lag(
    hours: Annotated[int, typer.Option(min=1, help="How far back to look.")] = 168,
) -> None:
    """Measure how long a provider takes to publish an observation.

    Freshness multipliers are currently an estimate. This reports the real gap
    between an observation's measurement time and the moment we first saw it,
    which is what those thresholds should actually be derived from. Accuracy is
    bounded by the poll interval, so run the collector continuously first.
    """
    import statistics

    _, product_config, repository = _runtime()
    rows = repository.publication_lag_samples(hours=hours)
    if not rows:
        typer.echo("no samples yet; run `swellsign collector` for a while first")
        return

    report: dict[str, object] = {"window_hours": hours, "stations": {}}
    for station_id, lags in sorted(rows.items()):
        ordered = sorted(lags)
        interval = product_config.stations[station_id].expected_interval_minutes or 0
        poll = product_config.station_interval_minutes(station_id, 20)
        median = statistics.median(ordered)
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        # Worst realistic age is the slowest publication plus one full
        # reporting cycle, since that is just before the next one lands.
        suggested_fresh = int(p90 + interval)
        report["stations"][station_id] = {
            "samples": len(ordered),
            "poll_interval_minutes": poll,
            "reporting_interval_minutes": interval,
            "publication_lag_minutes": {
                "min": round(min(ordered), 1),
                "median": round(median, 1),
                "p90": round(p90, 1),
                "max": round(max(ordered), 1),
            },
            "configured_fresh_minutes": (
                product_config.freshness_for(station_id).fresh_max_age_minutes
            ),
            "suggested_fresh_minutes": suggested_fresh,
        }
    typer.echo(json.dumps(report, indent=2))


@app.command("collector")
def collector() -> None:
    """Run observation, forecast, and tide collection on independent schedules."""
    settings, product_config, repository = _runtime()
    _configure_logging(settings.log_level)
    service = build_default_collection_service(settings, product_config, repository)

    async def run_service() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for caught_signal in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(caught_signal, stop.set)
        await service.run(
            observation_interval_minutes=settings.observation_interval_minutes,
            stop_event=stop,
        )

    lock_path = settings.database_path.with_suffix(".collector.lock")
    try:
        with _CollectorLock(lock_path):
            asyncio.run(run_service())
    except CollectorAlreadyRunning as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error


@app.command("snapshot")
def snapshot(
    spot_id: Annotated[str, typer.Argument(help="Configured spot identifier.")] = "new-smyrna",
) -> None:
    """Compose and print an observed snapshot from stored measurements."""
    _, product_config, repository = _runtime()
    result = SnapshotComposer(repository, product_config).compose(spot_id)
    typer.echo(result.model_dump_json(indent=2))


@app.command("api")
def api(
    host: Annotated[str | None, typer.Option(help="Bind host; LAN exposure is explicit.")] = None,
    port: Annotated[int | None, typer.Option(min=1, max=65535)] = None,
    reload: Annotated[bool, typer.Option(help="Reload source during local development.")] = False,
) -> None:
    """Serve the versioned observation and forecast API."""
    settings = get_settings()
    _configure_logging(settings.log_level)
    bind_host = host or settings.api_host
    bind_port = port or settings.api_port
    if reload:
        uvicorn.run(
            "swellsign.api:app",
            host=bind_host,
            port=bind_port,
            reload=True,
        )
        return
    from .api import create_app

    uvicorn.run(
        create_app(),
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
    )


@app.command("render-preview")
def render_preview(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output_path: Annotated[Path, typer.Argument(dir_okay=False)],
    scale: Annotated[int, typer.Option(min=1, max=20)] = 6,
    brightness: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.55,
    offline: Annotated[bool, typer.Option(help="Render the cached/offline status.")] = False,
) -> None:
    """Render a compact display JSON fixture to a pixel-perfect PNG."""
    rendered = render_json_file(
        input_path,
        output_path,
        scale=scale,
        brightness=brightness,
        offline=offline,
    )
    typer.echo(str(rendered))


@app.command("simulate")
def simulate(
    host: Annotated[str, typer.Option(help="Bind host for the simulator page.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8100,
    api_url: Annotated[
        str | None,
        typer.Option(help="Display endpoint to add a live state; fixtures work without it."),
    ] = None,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Serve the browser panel for the 128x32 face.

    Frames come from the same renderer that drives the Pi, so the page is an
    output adapter rather than a second implementation of the layout.
    """
    from .display.web import create_simulator_app

    settings = get_settings()
    _configure_logging(settings.log_level)
    url = f"http://{host}:{port}/"
    typer.echo(f"Swell Sign simulator on {url}")
    if api_url:
        typer.echo(f"Live state polls {api_url}")
    if open_browser:
        with suppress(Exception):
            webbrowser.open(url)
    uvicorn.run(
        create_simulator_app(api_url=api_url),
        host=host,
        port=port,
        log_level="warning",
    )


@app.command("write-state-fixtures")
def write_state_fixtures_command(
    directory: Annotated[Path, typer.Argument()] = Path("examples/states"),
) -> None:
    """Write every simulator state to JSON for golden-image and review work."""
    from .display.web import write_state_fixtures

    written = write_state_fixtures(directory)
    typer.echo(json.dumps([str(path) for path in written], indent=2))


@app.command("frame-tv")
def frame_tv(
    host: Annotated[str, typer.Option(help="The TV's IP address on your LAN.")],
    spot_id: Annotated[str, typer.Option()] = "new-smyrna",
    interval_minutes: Annotated[
        float,
        typer.Option(min=1.0, help="How often to push a new image."),
    ] = 15.0,
    brightness: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.55,
    cell: Annotated[int, typer.Option(min=4, max=40, help="LED size in pixels.")] = 20,
    token_file: Annotated[Path, typer.Option()] = Path("data/frame-tv-token.txt"),
    background: Annotated[
        Path | None,
        typer.Option(help="Photo to sit behind the sign. Omit for a dark field."),
    ] = None,
    credit: Annotated[
        str | None,
        typer.Option(help="Attribution line composited into the frame."),
    ] = None,
    placement: Annotated[
        str,
        typer.Option(help="Where the sign sits: center or lower."),
    ] = "center",
    background_dim: Annotated[
        float,
        typer.Option(min=0.0, max=1.0, help="Uniformly darken the photo. 0 leaves it alone."),
    ] = 0.0,
    layout: Annotated[
        str,
        typer.Option(help="sign (the instrument over a photo) or editorial (typographic)."),
    ] = "sign",
    once: Annotated[bool, typer.Option("--once", help="Push a single frame and exit.")] = False,
) -> None:
    """Push the rendered face to a Samsung Frame TV's Art Mode.

    Art Mode shows while the TV is in standby, so this is the ambient view.
    Nothing of ours runs on the TV; it is receiving pixels, exactly like the
    HUB75 panel does.
    """
    from .display.frame import render_frame_image
    from .display.frametv import FrameTvArtClient

    settings, product_config, repository = _runtime()
    _configure_logging(settings.log_level)
    composer = SnapshotComposer(repository, product_config)
    tide_service = TideContextService(repository, product_config)
    client = FrameTvArtClient(host=host, token_file=token_file)

    if not token_file.exists():
        typer.echo(
            f"First connection to {host}. The TV must be in Art Mode, not showing "
            "an input — the art channel does not answer otherwise. Some models "
            "also prompt on screen to allow the device; accept it if it appears.",
        )

    if not client.supported():
        typer.echo(
            f"{host} did not report Art Mode support. Confirm it is a Frame, that it is "
            "powered, and that this machine is on the same network.",
            err=True,
        )
        raise typer.Exit(code=1)

    # supported() is true for any Frame; it does not prove the art channel will
    # answer. Checking here converts a silent hang into an explanation.
    is_ready, detail = client.ready()
    if not is_ready:
        typer.echo(detail, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"connected to {host} ({detail})")

    def push_once() -> None:
        snapshot = composer.compose(spot_id)
        payload = compact_display_payload(
            snapshot,
            product_config,
            tide=tide_service.phase(spot_id),
        )
        if layout == "editorial":
            if background is None:
                typer.echo("editorial needs --background; it is a photograph layout", err=True)
                raise typer.Exit(code=1)

            from .display.editorial import render_editorial_image

            kts = product_config.display.wind_speed_unit == "kts"
            image = render_editorial_image(
                payload,
                background=background,
                place=product_config.spots[spot_id].name,
                credit=credit,
                speed_suffix="kts" if kts else "mph",
                speed_scale=0.868976242 if kts else 1.0,
            )
        else:
            image = render_frame_image(
                payload,
                cell=cell,
                brightness=brightness,
                background=background,
                credit=credit,
                placement=placement,
                background_dim=background_dim,
                display_config=product_config.display,
            )
        content_id = client.push(image)
        typer.echo(f"pushed {content_id or '(failed)'}")

    push_once()
    if once:
        return
    try:
        while True:
            time.sleep(interval_minutes * 60)
            push_once()
    except KeyboardInterrupt:
        pass


@app.command("display")
def display(
    api_url: Annotated[
        str,
        typer.Option(help="Versioned /display endpoint accepting observed data only."),
    ] = "http://127.0.0.1:8000/v1/spots/new-smyrna/display",
    cache_path: Annotated[Path, typer.Option()] = Path("data/display-cache.json"),
    interval_seconds: Annotated[float, typer.Option(min=1.0, help="API poll cadence.")] = 15.0,
    frames_per_second: Annotated[float, typer.Option(min=1.0, max=60.0)] = 20.0,
    motion: Annotated[
        bool,
        typer.Option("--motion/--no-motion", help="Period-paced one-pixel crest."),
    ] = True,
    brightness: Annotated[
        float | None,
        typer.Option(min=0.0, max=1.0, help="Pin brightness and ignore the schedule."),
    ] = None,
    day_brightness: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.55,
    evening_brightness: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.40,
    night_brightness: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.28,
    matrix_brightness: Annotated[int, typer.Option(min=1, max=100)] = 35,
) -> None:
    """Poll the API and drive two chained 64x32 Raspberry Pi panels.

    Frame rendering is deliberately decoupled from API polling. The upstream
    cadence is minutes, the poll is seconds, and the crest needs to complete
    one cycle per reported dominant period, so all three run independently.
    """
    client = DisplayClient(api_url, cache_path)
    renderer = DisplayRenderer.for_display(
        get_product_config().display,
        brightness=day_brightness if brightness is None else brightness,
    )
    output = PiMatrixOutput(brightness=matrix_brightness)
    controller = (
        None
        if brightness is not None
        else BrightnessController(
            BrightnessSchedule(
                day=day_brightness,
                evening=evening_brightness,
                night=night_brightness,
            )
        )
    )

    frame_interval = 1.0 / frames_per_second
    payload = None
    next_poll = 0.0
    started = time.monotonic()

    try:
        while True:
            frame_started = time.monotonic()
            if frame_started >= next_poll:
                payload = client.fetch()
                next_poll = frame_started + interval_seconds

            if payload is not None:
                # Recalculate every frame so the age keeps climbing and the
                # freshness stays honest through a long API outage.
                frame_payload = recalculate_ages(payload)
                if controller is not None:
                    renderer.brightness = controller.advance(datetime.now().astimezone().hour)
                output.draw(
                    renderer.render(
                        frame_payload,
                        offline=client.offline,
                        animation_phase=animation_phase(
                            frame_payload,
                            time.monotonic() - started,
                            motion=motion,
                        ),
                    )
                )

            time.sleep(max(0.0, frame_interval - (time.monotonic() - frame_started)))
    except KeyboardInterrupt:
        pass
    finally:
        output.clear()


if __name__ == "__main__":
    app()
