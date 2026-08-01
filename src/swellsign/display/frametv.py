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
import json
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
    # samsungtvws defaults to no timeout, which blocks forever on the first
    # connection while the TV waits for someone to accept its on-screen
    # pairing prompt. Long enough to walk to the remote, short enough to fail.
    timeout_seconds: float = 45.0
    # Uploaded ids must outlive the process. Held only in memory, a `--once`
    # run exits having forgotten what it just uploaded, so retirement never
    # runs and every push accumulates on the TV forever.
    #
    # This file is also the safety boundary for deletion: nothing is ever
    # removed unless we recorded uploading it. The owner's own photographs and
    # Art Store purchases share the same MY_F identifier space, so "looks like
    # ours" is not good enough to delete on.
    uploads_file: Path | None = None
    _uploaded: list[str] = field(default_factory=list, init=False)
    _tv: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._uploaded = self._load_uploads()

    def _load_uploads(self) -> list[str]:
        if self.uploads_file is None or not self.uploads_file.exists():
            return []
        try:
            data = json.loads(self.uploads_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("upload manifest unreadable; starting empty")
            return []
        return [str(item) for item in data.get("uploaded", [])]

    def _save_uploads(self) -> None:
        if self.uploads_file is None:
            return
        try:
            self.uploads_file.parent.mkdir(parents=True, exist_ok=True)
            self.uploads_file.write_text(
                json.dumps({"host": self.host, "uploaded": self._uploaded}, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("could not persist upload manifest", extra={"error": str(error)})

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
        self._tv = SamsungTVWS(
            host=self.host,
            port=self.port,
            token_file=token,
            timeout=self.timeout_seconds,
            # This string is what the TV shows in its "allow this device?"
            # prompt. The library default is "SamsungTvRemote", which is easy
            # to mistake for something unwanted and decline.
            name="Swell Sign",
        )
        return self._tv

    def supported(self) -> bool:
        """True when this really is a Frame with Art Mode."""
        try:
            return bool(self._connect().art().supported())
        except Exception as error:
            logger.warning("art mode probe failed", extra={"error": str(error)})
            return False

    def ready(self) -> tuple[bool, str]:
        """Check the art channel actually answers, not merely that it exists.

        `supported()` reads REST device info and returns True on any Frame, even
        when art commands will never reply. Verified on a 2025 LS03F: with the
        TV powered on but showing an input, the websocket connects and registers
        a client, then every art command times out silently. The channel only
        responds once the TV is in Art Mode.

        This turns that hang into a sentence.
        """
        try:
            art = self._connect().art()
        except RuntimeError:
            raise
        except Exception as error:
            return False, f"could not reach {self.host}: {error}"

        try:
            version = art.get_api_version()
        except Exception:
            return False, (
                f"{self.host} accepted the connection but its art channel did not "
                "answer. The TV is most likely powered on showing an input rather "
                "than in Art Mode; the channel only responds in Art Mode. Press the "
                "power button once to switch to Art Mode, then retry."
            )
        return True, f"art api {version}"

    def push(self, image: Image.Image, *, show: bool = True) -> str | None:
        """Upload one frame, optionally select it, and retire older uploads."""
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        payload = buffer.getvalue()

        try:
            art = self._connect().art()
            content_id = art.upload(
                payload,
                file_type="png",
                matte=self.matte,
                # Left unset this defaults to shadowbox_polar, which would
                # frame an already-framed 16:9 render a second time.
                portrait_matte=self.matte,
            )
        except Exception as error:
            logger.warning("frame upload failed", extra={"error": str(error)})
            return None

        if not content_id:
            return None
        self._uploaded.append(content_id)
        # Persist before selecting: if anything below fails, the id is already
        # recorded and a later run can still retire it.
        self._save_uploads()

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
        self._save_uploads()

    def purge(self, *, keep_displayed: bool = True) -> tuple[list[str], list[str]]:
        """Delete every image this client recorded uploading.

        Only ids present in the manifest are touched. Anything the owner put on
        the TV is invisible to this method by construction, which is the point:
        personal photographs and our frames share one identifier space, so
        pattern-matching identifiers would eventually delete someone's holiday.

        Returns the ids removed and the ids that failed.
        """
        if not self._uploaded:
            return [], []
        art = self._connect().art()

        if keep_displayed:
            # Deleting the image currently on screen can leave Art Mode on a
            # blank slot, so move to something that is not ours first.
            try:
                current = str(art.get_current().get("content_id", ""))
                if current in self._uploaded:
                    others = [
                        str(i.get("content_id"))
                        for i in art.available()
                        if str(i.get("content_id")) not in self._uploaded
                    ]
                    if others:
                        art.select_image(sorted(others)[-1], show=True)
            except Exception as error:
                logger.warning("could not reselect before purge", extra={"error": str(error)})

        removed, failed = [], []
        for content_id in list(self._uploaded):
            try:
                art.delete(content_id)
                removed.append(content_id)
                self._uploaded.remove(content_id)
            except Exception as error:
                failed.append(content_id)
                logger.warning(
                    "purge delete failed",
                    extra={"content_id": content_id, "error": str(error)},
                )
        self._save_uploads()
        return removed, failed
