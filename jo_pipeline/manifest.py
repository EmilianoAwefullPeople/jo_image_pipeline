import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

MEDIA_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
}
UNKNOWN_MEDIA_TYPE = "unknown"
HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    media_type: str
    size_bytes: int
    sha256: str
    modified_utc: str


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    dataset_version: str
    source_root: str
    created_utc: str
    entries: list[ManifestEntry]

    def to_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "source_root": self.source_root,
            "created_utc": self.created_utc,
            "entries": [asdict(entry) for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "DatasetManifest":
        return cls(
            dataset_id=payload["dataset_id"],
            dataset_version=payload["dataset_version"],
            source_root=payload["source_root"],
            created_utc=payload["created_utc"],
            entries=[ManifestEntry(**entry) for entry in payload["entries"]],
        )


class InventoryScanner:
    def __init__(self, dataset_root: Path, manifest_dir: Path):
        self.dataset_root = dataset_root
        self.manifest_dir = manifest_dir

    def scan(self, dataset_id: str, dataset_version: str) -> DatasetManifest:
        source_root = self.dataset_root / dataset_id
        if not source_root.is_dir():
            raise FileNotFoundError(f"{dataset_id}: dataset folder not found at {source_root}")

        entries = self._collect_entries(dataset_id, source_root)
        LOGGER.info(f"{dataset_id}: inventory scanned {len(entries)} files under {source_root}")
        return DatasetManifest(
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            source_root=str(source_root),
            created_utc=datetime.now(timezone.utc).isoformat(),
            entries=entries,
        )

    def manifest_path(self, dataset_id: str, dataset_version: str) -> Path:
        return self.manifest_dir / f"{dataset_id}-v{dataset_version}.json"

    def write(self, manifest: DatasetManifest) -> Path:
        target = self.manifest_path(manifest.dataset_id, manifest.dataset_version)
        if target.exists():
            raise FileExistsError(f"{manifest.dataset_id}: manifest {target} already exists, bump the dataset version")

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest.to_dict(), indent=2))
        LOGGER.info(f"{manifest.dataset_id}: manifest v{manifest.dataset_version} written to {target}")
        return target

    def load(self, dataset_id: str, dataset_version: str) -> DatasetManifest:
        source = self.manifest_path(dataset_id, dataset_version)
        if not source.is_file():
            raise FileNotFoundError(f"{dataset_id}: manifest not found at {source}")

        manifest = DatasetManifest.from_dict(json.loads(source.read_text()))
        LOGGER.debug(f"{dataset_id}: manifest v{dataset_version} loaded with {len(manifest.entries)} entries")
        return manifest

    def _collect_entries(self, dataset_id: str, source_root: Path) -> list[ManifestEntry]:
        entries = []
        for path in sorted(source_root.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                entries.append(self._build_entry(dataset_id, source_root, path))
            else:
                LOGGER.debug(f"{dataset_id}: skipped non-media path {path}")
        return entries

    def _build_entry(self, dataset_id: str, source_root: Path, path: Path) -> ManifestEntry:
        relative_path = str(path.relative_to(source_root))
        media_type = MEDIA_TYPES.get(path.suffix.lower(), UNKNOWN_MEDIA_TYPE)
        stats = path.stat()
        if media_type == UNKNOWN_MEDIA_TYPE:
            LOGGER.info(f"{dataset_id}: {relative_path} has unrecognised extension {path.suffix}, recorded as unknown")
        else:
            LOGGER.debug(f"{dataset_id}: {relative_path} recorded as {media_type}")

        return ManifestEntry(
            relative_path=relative_path,
            media_type=media_type,
            size_bytes=stats.st_size,
            sha256=self._hash_file(path),
            modified_utc=datetime.fromtimestamp(stats.st_mtime, timezone.utc).isoformat(),
        )

    def _hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest()
