"""Helpers for native and enlarged physical-display previews."""

from __future__ import annotations

import json
from pathlib import Path

from ..models import CompactDisplayPayload
from .renderer import DisplayRenderer


def render_json_file(
    input_path: Path | str,
    output_path: Path | str,
    *,
    scale: int = 6,
    brightness: float = 0.55,
    offline: bool = False,
) -> Path:
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    payload = CompactDisplayPayload.model_validate(raw)
    return DisplayRenderer(brightness=brightness).save(
        payload,
        output_path,
        scale=scale,
        offline=offline,
    )

