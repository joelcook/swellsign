import pytest
from typer.testing import CliRunner

from swellsign.cli import CollectorAlreadyRunning, _CollectorLock, app

runner = CliRunner()


def test_render_preview_command(tmp_path):
    output = tmp_path / "sign.png"
    result = runner.invoke(
        app,
        [
            "render-preview",
            "examples/display-fresh.json",
            str(output),
            "--scale",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    assert output.stat().st_size > 0


def test_help_names_the_observation_contract():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "without scoring" in result.output


def test_collector_process_lock_is_exclusive(tmp_path):
    lock_path = tmp_path / "collector.lock"
    with (
        _CollectorLock(lock_path),
        pytest.raises(CollectorAlreadyRunning),
        _CollectorLock(lock_path),
    ):
        raise AssertionError("second collector acquired the lock")
