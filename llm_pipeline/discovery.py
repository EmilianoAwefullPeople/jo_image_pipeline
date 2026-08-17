import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DiscoveredImage:
    relative_path: str
    sha256: str
    size_bytes: int


def discover_images(dataset_path: Path) -> list[DiscoveredImage]:
    if not dataset_path.is_dir():
        raise FileNotFoundError(f"{dataset_path.name}: dataset folder not found at {dataset_path}")

    images = []
    for path in sorted(dataset_path.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        relative_path = str(path.relative_to(dataset_path))
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            LOGGER.debug(f"{relative_path}: skipped, extension {path.suffix} is not an evaluable image")
            continue
        images.append(DiscoveredImage(relative_path=relative_path, sha256=_hash_file(path), size_bytes=path.stat().st_size))
        LOGGER.debug(f"{relative_path}: discovered for evaluation")

    LOGGER.info(f"{dataset_path.name}: discovered {len(images)} images for evaluation")
    return images


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
