import json
import logging
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from jo_pipeline.assets import AssetSignals
from jo_pipeline.extract import hamming_distance, image_difference_hash

LOGGER = logging.getLogger(__name__)

REFERENCE_VERSION = "reference-1"
MATCH_MAX_BITS = 6
DOCUMENT_PART = "word/document.xml"
RELATIONSHIP_PART = "word/_rels/document.xml.rels"
MEDIA_PREFIX = "word/media/"
EXCLUDED_HEADING = "intentionally left out"
NARRATIVE_HEADING = "memories unpacked"

PARAGRAPH_PATTERN = re.compile(r"<w:p[ >].*?</w:p>", re.S)
EMBED_PATTERN = re.compile(r'r:embed="([^"]+)"')
RELATIONSHIP_PATTERN = re.compile(r'Id="([^"]+)"[^>]*Target="media/([^"]+)"')
TAG_PATTERN = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class ReferenceGroup:
    index: int
    asset_paths: list[str]
    unmatched_media: list[str]


@dataclass(frozen=True)
class ReferenceGrouping:
    dataset_id: str
    method_version: str
    groups: list[ReferenceGroup]
    excluded_paths: list[str]
    unmatched_media: list[str]

    def grouped_paths(self) -> set:
        return {path for group in self.groups for path in group.asset_paths}


class ReferenceReader:
    def __init__(self, document_path: Path):
        self.document_path = document_path

    def read(self, dataset_id: str, signals: list[AssetSignals]) -> ReferenceGrouping:
        with zipfile.ZipFile(self.document_path) as archive:
            relationships = self._read_relationships(archive)
            document = archive.read(DOCUMENT_PART).decode("utf-8")
            media_hashes = self._read_media_hashes(archive)

        groups = []
        excluded_media = []
        unmatched = []
        section = "grouped"
        for paragraph in PARAGRAPH_PATTERN.findall(document):
            heading = TAG_PATTERN.sub("", paragraph).strip().lower()
            if heading.startswith(EXCLUDED_HEADING):
                section = "excluded"
            elif heading.startswith(NARRATIVE_HEADING):
                section = "narrative"

            media_names = [relationships[embed] for embed in EMBED_PATTERN.findall(paragraph) if embed in relationships]
            if not media_names or section == "narrative":
                continue

            matched = [self._match(name, media_hashes, signals) for name in media_names]
            resolved = [path for path in matched if path]
            unmatched.extend([name for name, path in zip(media_names, matched) if not path])
            if section == "excluded":
                excluded_media.extend(resolved)
            else:
                groups.append(ReferenceGroup(
                    index=len(groups) + 1,
                    asset_paths=resolved,
                    unmatched_media=[name for name, path in zip(media_names, matched) if not path],
                ))

        LOGGER.info(f"{dataset_id}: reference document yielded {len(groups)} groups, {len(excluded_media)} excluded and {len(unmatched)} unmatched images")
        return ReferenceGrouping(
            dataset_id=dataset_id,
            method_version=REFERENCE_VERSION,
            groups=groups,
            excluded_paths=excluded_media,
            unmatched_media=unmatched,
        )

    def _read_relationships(self, archive: zipfile.ZipFile) -> dict:
        return dict(RELATIONSHIP_PATTERN.findall(archive.read(RELATIONSHIP_PART).decode("utf-8")))

    def _read_media_hashes(self, archive: zipfile.ZipFile) -> dict:
        hashes = {}
        for name in archive.namelist():
            if name.startswith(MEDIA_PREFIX):
                with Image.open(BytesIO(archive.read(name))) as image:
                    hashes[Path(name).name] = image_difference_hash(image)
        return hashes

    def _match(self, media_name: str, media_hashes: dict, signals: list[AssetSignals]) -> str | None:
        media_hash = media_hashes.get(media_name)
        if not media_hash:
            return None

        distances = {asset.relative_path: hamming_distance(media_hash, asset.difference_hash) for asset in signals if asset.difference_hash}
        best = min(distances, key=distances.get)
        if distances[best] > MATCH_MAX_BITS:
            LOGGER.info(f"{media_name}: no dataset photo within {MATCH_MAX_BITS} bits, closest was {best} at {distances[best]} bits")
            return None

        LOGGER.debug(f"{media_name}: matched {best} at {distances[best]} bits")
        return best


class JsonReferenceReader:
    def __init__(self, document_path: Path):
        self.document_path = document_path

    def read(self, dataset_id: str, signals: list[AssetSignals]) -> ReferenceGrouping:
        payload = json.loads(self.document_path.read_text())
        known_paths = {asset.relative_path for asset in signals}

        groups = []
        unmatched = []
        for entry in payload["groups"]:
            resolved = [path for path in entry["asset_paths"] if path in known_paths]
            missing = [path for path in entry["asset_paths"] if path not in known_paths]
            unmatched.extend(missing)
            groups.append(ReferenceGroup(index=len(groups) + 1, asset_paths=resolved, unmatched_media=missing))

        excluded = [path for path in payload["excluded_paths"] if path in known_paths]
        unmatched.extend(path for path in payload["excluded_paths"] if path not in known_paths)
        LOGGER.info(f"{dataset_id}: reference file yielded {len(groups)} groups, {len(excluded)} excluded and {len(unmatched)} unmatched images")
        return ReferenceGrouping(
            dataset_id=dataset_id,
            method_version=REFERENCE_VERSION,
            groups=groups,
            excluded_paths=excluded,
            unmatched_media=unmatched,
        )


def reference_reader(document_path: Path) -> ReferenceReader | JsonReferenceReader:
    if document_path.suffix.lower() == ".json":
        return JsonReferenceReader(document_path)
    return ReferenceReader(document_path)
