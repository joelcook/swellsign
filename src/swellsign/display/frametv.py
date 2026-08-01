"""Push the rendered face to a Samsung Frame TV's Art Mode.

Art Mode is not an application runtime. Nothing of ours executes on the TV; it
displays images, and this uploads one. That makes the Frame the same kind of
client as the HUB75 panel and the browser: it receives pixels rendered here.

The Samsung WebSocket API is unofficial and undocumented. Samsung can change it
without notice, so every call is treated as best-effort and a failure never
propagates far enough to disturb collection or the API.

Requires the optional dependency:

    pip install 'swellsign[frame]'
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# The TV stores roughly 1200 images. Uploading every few minutes would fill it
# within days, so we delete our own previous uploads and keep only a small
# rolling window. Anything not uploaded by us is never touched: the Art Store
# purchases and personal photos on that TV are not ours to manage.
DEFAULT_KEEP = 2


@dataclass
class FrameTvArtClient:
    """Upload rendered frames and retire the ones we previously uploaded."""

    host: str
    port: int = 8002
    token_file: Path | None = None
    keep: int = DEFAULT_KEEP
    matte: str = "none"
    _uploaded: list[str] = field(default_factory=list, init=False)
    _tv: Any = field(default=None, init=False)

    def _connect(self) -> Any:
        if self._tv is not None:
            return self._tv
        try:
            from samsungtvws import SamsungTVWS
        except ImportError as error:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Install the Frame extra to push to a Samsung TV: "
                "pip install 'swellsign[frame]'"
            ) from error

        # A token file lets the TV remember the pairing, so the on-screen
        # "allow this device?" prompt appears once rather than every run.
        token = str(self.token_file) if self.token_file else None
        self._tv = SamsungTVWS(host=self.host, port=self.port, token_file=token)
        return self._tv

    def supported(self) -> bool:
        """True when this really is a Frame with Art Mode."""
        try:
            return bool(self._connect().art().supported())
        except Exception as error:
            logger.warning("art mode probe failed", extra={"error": str(error)})
            return False

    def push(self, image: Image.Image, *, show: bool = True) -> str | None:
        """Upload one frame, optionally select it, and retire older uploads."""
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        payload = buffer.getvalue()

        try:
            art = self._connect().art()
            content_id = art.upload(payload, file_type="PNG", matte=self.matte)
        except Exception as error:
            logger.warning("frame upload failed", extra={"error": str(error)})
            return None

        if not content_id:
            return None
        self._uploaded.append(content_id)

        if show:
            try:
                art.select_image(content_id, show=True)
            except Exception as error:
                logger.warning("frame select failed", extra={"error": str(error)})

        self._retire(art)
        return content_id

    def _retire(self, art: Any) -> None:
        """Delete our older uploads, keeping a small window.

        The newest is kept because it is on screen, and one behind it because
        deleting an image the TV is still switching to can leave Art Mode on a
        blank slot.
        """
        while len(self._uploaded) > max(1, self.keep):
            stale = self._uploaded.pop(0)
            try:
                art.delete(stale)
            except Exception as error:
                logger.warning(
                    "frame delete failed; it will need clearing by hand",
                    extra={"content_id": stale, "error": str(error)},
                )
