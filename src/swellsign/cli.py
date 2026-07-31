"""Command-line operations for the backend, simulator, and physical sign."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import signal
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .config import get_product_config, get_settings
from .display.client import DisplayClient
from .display.hub75 import PiMatrixOutput
from .display.renderer import DisplayRenderer
from .display.simulator import render_json_file
from .services.collector import build_default_collection_service
from .services.snapshot import SnapshotComposer
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
) -> None:
    """Fetch current observation sources once, with optional forecast archive."""
    settings, product_config, repository = _runtime()
    _configure_logging(settings.log_level)
    service = build_default_collection_service(settings, product_config, repository)

    async def run_once() -> list[dict[str, object]]:
        results = [asdict(await service.collect_observations_once())]
        if forecast:
            results.append(asdict(await service.collect_forecast_once()))
        return results

    typer.echo(json.dumps(asyncio.run(run_once()), default=str, indent=2))


@app.command("collector")
def collector() -> None:
    """Run observation and forecast collection on independent schedules."""
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


@app.command("display")
def display(
    api_url: Annotated[
        str,
        typer.Option(help="Versioned /display endpoint accepting observed data only."),
    ] = "http://127.0.0.1:8000/v1/spots/new-smyrna/display",
    cache_path: Annotated[Path, typer.Option()] = Path("data/display-cache.json"),
    interval_seconds: Annotated[float, typer.Option(min=1.0)] = 15.0,
    pixel_brightness: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.55,
    matrix_brightness: Annotated[int, typer.Option(min=1, max=100)] = 35,
) -> None:
    """Poll the API and drive two chained 64x32 Raspberry Pi panels."""
    client = DisplayClient(api_url, cache_path)
    renderer = DisplayRenderer(brightness=pixel_brightness)
    output = PiMatrixOutput(brightness=matrix_brightness)
    try:
        while True:
            payload = client.fetch()
            if payload is not None:
                output.draw(renderer.render(payload, offline=client.offline))
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        output.clear()


if __name__ == "__main__":
    app()
