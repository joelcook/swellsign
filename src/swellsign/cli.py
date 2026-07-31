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
from .services.snapshot import SnapshotComposer
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
    renderer = DisplayRenderer(brightness=day_brightness if brightness is None else brightness)
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
