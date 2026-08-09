import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jo_pipeline.extract import IMAGE_MEDIA_PREFIX, PhotoExtractor
from jo_pipeline.manifest import DatasetManifest, ManifestEntry
from jo_pipeline.normalize import MetadataNormalizer, MetadataObservation

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedAsset:
    entry: ManifestEntry
    observations: list[MetadataObservation]
    failure: str | None

    def values(self) -> dict:
        return {observation.field: observation.value for observation in self.observations}


@dataclass(frozen=True)
class AssetSignals:
    relative_path: str
    sha256: str
    captured_utc: datetime | None
    latitude: float | None
    longitude: float | None
    blur_score: float | None
    difference_hash: str | None


class AssetLoader:
    def __init__(self, source_root: Path):
        self.extractor = PhotoExtractor(source_root)
        self.normalizer = MetadataNormalizer()

    def load(self, manifest: DatasetManifest) -> list[LoadedAsset]:
        loaded = []
        for entry in manifest.entries:
            if not entry.media_type.startswith(IMAGE_MEDIA_PREFIX):
                LOGGER.info(f"{entry.relative_path}: skipped, media type {entry.media_type} is not an image")
                continue

            extraction = self.extractor.extract(entry)
            if extraction.failures:
                loaded.append(LoadedAsset(entry=entry, observations=[], failure=extraction.failures[0]))
                continue

            loaded.append(LoadedAsset(entry=entry, observations=self.normalizer.normalize(extraction), failure=None))

        LOGGER.info(f"{manifest.dataset_id}: loaded {len(loaded)} image assets from manifest v{manifest.dataset_version}")
        return loaded


def build_signals(asset: LoadedAsset) -> AssetSignals:
    values = asset.values()
    captured = values.get("capture_timestamp_utc")
    return AssetSignals(
        relative_path=asset.entry.relative_path,
        sha256=asset.entry.sha256,
        captured_utc=datetime.fromisoformat(captured) if captured else None,
        latitude=values.get("gps_latitude"),
        longitude=values.get("gps_longitude"),
        blur_score=values.get("blur_score"),
        difference_hash=values.get("difference_hash"),
    )
