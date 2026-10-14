"""The ingestion contract: what a capture is, where photos land, and which backends exist.

Fase 1 has ONE real ingestion mode, phone_web, and it is PUSH-based: the phone's browser
POSTs the photo to /api/upload, so nothing on the server pulls frames. pi_csi and usb
arrive with Piloto 1 (Build Plan section 12) over the SAME upload endpoint: the brain
never changes when a backend is born. Here they are stubs that say so loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

# The photos root inside every container (compose volume photos:/data/photos). Rows store
# paths RELATIVE to it (<site>/<yyyymmdd>/<uuid>.jpg); the root itself never enters a row.
PHOTO_ROOT = Path("/data/photos")
REFERENCE_PREFIX = "reference/"        # the sacred folder: a plate's deletion never touches anything under it

INGESTION_MODES = ("phone_web", "pi_csi", "usb")

log = logging.getLogger("lpq.photos")


def unlink_plate_photo(rel_path: str) -> bool:
    """Delete one plate's photo for good (the worker's purge and the mostrador's eliminar-ya share this ONE
    guard, Section 3.14). Anything under reference/ (the sacred originals and the light copies) is refused by
    construction, and so is any path that tries to leave the photos root. A file already gone is fine; one that
    cannot be deleted (a folder, a permission) is logged and left, so the caller's other work goes on."""
    path = Path(rel_path)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.parts[0] + "/" == REFERENCE_PREFIX:
        log.error("refused to delete a protected path: %s", rel_path)
        return False
    try:
        (PHOTO_ROOT / rel_path).unlink(missing_ok=True)
    except OSError as exc:
        log.error("could not delete %s: %s", rel_path, exc.__class__.__name__)
        return False
    return True


@dataclass(frozen=True)
class Capture:
    """One captured frame ready for upload: the contract every backend must produce."""

    site: str
    camera: str | None
    record_type: str          # 'return' | 'outgoing'
    jpeg: bytes


class CaptureBackend:
    """Base contract. capture() hands back one Capture; pull-based backends implement it."""

    name: str = "abstract"

    def capture(self) -> Capture:
        raise NotImplementedError(f"{self.name}: capture() is not implemented")


class PhoneWebBackend(CaptureBackend):
    """Push-based: photos arrive through POST /api/upload from the phone's browser."""

    name = "phone_web"

    def capture(self) -> Capture:
        raise NotImplementedError(
            "phone_web is push-based: the phone POSTs photos to /api/upload; "
            "there is nothing to pull server-side"
        )


class PiCsiBackend(CaptureBackend):
    """Raspberry Pi camera module (picamera2, locked focus and white balance): Piloto 1."""

    name = "pi_csi"

    def capture(self) -> Capture:
        raise NotImplementedError(
            "pi_csi capture backend arrives with Piloto 1 (Build Plan section 12); "
            "Fase 1 ships the contract only"
        )


class UsbBackend(CaptureBackend):
    """USB webcam backup (Logitech C920/C922): Piloto 1."""

    name = "usb"

    def capture(self) -> Capture:
        raise NotImplementedError(
            "usb capture backend arrives with Piloto 1 (Build Plan section 12); "
            "Fase 1 ships the contract only"
        )


_BACKENDS: dict[str, type[CaptureBackend]] = {
    PhoneWebBackend.name: PhoneWebBackend,
    PiCsiBackend.name: PiCsiBackend,
    UsbBackend.name: UsbBackend,
}


def get_backend(name: str) -> CaptureBackend:
    """Resolve an ingestion mode from config/sites to its backend, or fail with the valid names."""
    try:
        return _BACKENDS[name]()
    except KeyError:
        raise ValueError(f"unknown ingestion mode {name!r}; valid: {INGESTION_MODES}") from None
