import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTContainer, LTImage, LTTextLine
from pdfminer.pdftypes import resolve1
from PIL import Image, ImageOps

from jo_pipeline.config import REPO_ROOT
from jo_pipeline.logging_setup import configure_logging

LOGGER = logging.getLogger(__name__)

EVAL_SETS_DIR = REPO_ROOT / "resources" / "final_eval_sets"
REFERENCES_DIR = EVAL_SETS_DIR / "references"
OVERRIDES_PATH = REFERENCES_DIR / "match_overrides.json"
DEBUG_THUMBS_DIR = REPO_ROOT / "data" / "reference_thumbs"
SOURCES = (
    ("JO_Manual_Grouping_Reference.pdf", "moments"),
    ("JO_Semantic_Content_Groups.pdf", "semantic"),
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}

TRIP_PATTERN = re.compile(r"^Trip\s+\d+:\s+(?P<name>.+)$")
GROUP_PATTERN = re.compile(r"^(?P<title>.+?)\s+[—–-]\s+(?P<count>\d+)\s+photo\(s\)$")
EXCLUDED_MARKER = "excluded / not used"
COMPARE_EDGE = 32
TRIM_KEEP_FRACTION = 0.65
ACCEPT_MAX_DIFFERENCE = 18.0
VISUAL_CHECK_DIFFERENCE = 10.0


@dataclass
class ReferenceGroupDraft:
    index: int
    title: str
    expected_count: int
    excluded: bool
    placements: list = field(default_factory=list)


@dataclass
class TripDraft:
    folder_name: str
    groups: list = field(default_factory=list)


@dataclass
class Placement:
    variant: str
    trip: str
    group_index: int
    position: int
    raster: Image.Image
    matched_path: str | None = None
    match_difference: float | None = None
    via_override: bool = False

    def key(self) -> str:
        return f"{self.variant}:{self.trip}:{self.group_index}:{self.position}"


def normalise_trip_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFD", name.strip())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalise_line(line: LTTextLine) -> str:
    return " ".join(line.get_text().split())


def walk_layout(element, images: list, lines: list):
    if isinstance(element, LTImage):
        images.append(element)
    elif isinstance(element, LTTextLine):
        lines.append(element)
    elif isinstance(element, LTContainer):
        for child in element:
            walk_layout(child, images, lines)


def decode_raster(item: LTImage) -> Image.Image:
    stream = item.stream
    data = stream.get_data()
    width = resolve1(stream.get_any(("W", "Width")))
    height = resolve1(stream.get_any(("H", "Height")))
    depth = resolve1(stream.get_any(("BPC", "BitsPerComponent")))
    if depth != 8:
        raise ValueError(f"{item.name}: unsupported bits per component {depth}")

    pixels = width * height
    components = len(data) // pixels
    if components not in (1, 3) or components * pixels != len(data):
        raise ValueError(f"{item.name}: cannot map {len(data)} bytes onto {width}x{height} pixels")

    mode = "RGB" if components == 3 else "L"
    return Image.frombytes(mode, (width, height), data)


def parse_pdf(pdf_path: Path, variant: str) -> dict[str, TripDraft]:
    trips = {}
    current_trip = None
    current_group = None
    for page in extract_pages(pdf_path):
        images = []
        lines = []
        walk_layout(page, images, lines)
        items = [(line.bbox[3], line.bbox[0], "line", line) for line in lines]
        items.extend((image.bbox[3], image.bbox[0], "image", image) for image in images)
        for top, left, kind, item in sorted(items, key=lambda entry: (-entry[0], entry[1])):
            if kind == "line":
                text = normalise_line(item)
                trip_match = TRIP_PATTERN.match(text)
                group_match = GROUP_PATTERN.match(text)
                if trip_match:
                    folder_name = normalise_trip_name(trip_match.group("name"))
                    current_trip = trips.setdefault(folder_name, TripDraft(folder_name=folder_name))
                    current_group = None
                    LOGGER.info(f"{pdf_path.name}: reading trip section {text} as dataset {folder_name}")
                elif group_match and current_trip:
                    current_group = ReferenceGroupDraft(
                        index=len(current_trip.groups) + 1,
                        title=group_match.group("title"),
                        expected_count=int(group_match.group("count")),
                        excluded=EXCLUDED_MARKER in group_match.group("title").lower(),
                    )
                    current_trip.groups.append(current_group)
            else:
                if not current_group:
                    raise ValueError(f"{pdf_path.name}: image placement before any group header on a {current_trip.folder_name if current_trip else 'preamble'} page")
                current_group.placements.append(Placement(
                    variant=variant,
                    trip=current_trip.folder_name,
                    group_index=current_group.index,
                    position=len(current_group.placements) + 1,
                    raster=decode_raster(item),
                ))
    return trips


def compare_array(image: Image.Image) -> numpy.ndarray:
    return numpy.asarray(image.convert("RGB").resize((COMPARE_EDGE, COMPARE_EDGE), Image.Resampling.BILINEAR), dtype=numpy.float32)


def square_crops(image: Image.Image) -> list[Image.Image]:
    edge = min(image.size)
    spare_x = image.width - edge
    spare_y = image.height - edge
    offsets = {(spare_x // 2, spare_y // 2), (0, 0), (spare_x, spare_y)}
    return [image.crop((x, y, x + edge, y + edge)) for x, y in sorted(offsets)]


def candidate_variants(folder: Path) -> dict[str, list[numpy.ndarray]]:
    variants = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        with Image.open(path) as image:
            upright = ImageOps.exif_transpose(image)
            variants[path.name] = [compare_array(crop) for crop in square_crops(upright)] + [compare_array(upright)]
    return variants


def trimmed_difference(thumb: numpy.ndarray, candidate: numpy.ndarray) -> float:
    differences = numpy.sort(numpy.abs(thumb - candidate).mean(axis=2).ravel())
    kept = differences[: int(len(differences) * TRIM_KEEP_FRACTION)]
    return float(kept.mean())


def assign_placements(placements: list[Placement], candidates: dict[str, list[numpy.ndarray]], overrides: dict, used: set) -> list[tuple[Placement, str]]:
    pending = []
    for placement in placements:
        override = overrides.get(placement.key())
        if override:
            if override not in candidates:
                raise ValueError(f"{placement.key()}: override names {override}, which is not a file in the {placement.trip} folder")
            placement.matched_path = override
            placement.via_override = True
            used.add(override)
        else:
            pending.append(placement)

    scores = {placement.key(): {name: min(trimmed_difference(compare_array(placement.raster), variant) for variant in variants) for name, variants in candidates.items()} for placement in pending}
    entries = sorted((score, placement.key(), name, placement) for placement in pending for name, score in scores[placement.key()].items())
    for score, key, name, placement in entries:
        if placement.matched_path or name in used or score > ACCEPT_MAX_DIFFERENCE:
            continue
        placement.matched_path = name
        placement.match_difference = score
        used.add(name)
        if score > VISUAL_CHECK_DIFFERENCE:
            LOGGER.info(f"{placement.key()}: matched {name} at difference {score:.1f}, above {VISUAL_CHECK_DIFFERENCE} so verify visually")
        else:
            LOGGER.debug(f"{placement.key()}: matched {name} at difference {score:.1f}")

    unresolved = []
    for placement in pending:
        if not placement.matched_path:
            free_scores = sorted((score, name) for name, score in scores[placement.key()].items() if name not in used)
            closest = f"closest free {free_scores[0][1]} at {free_scores[0][0]:.1f}" if free_scores else "no free files left"
            unresolved.append((placement, closest))
    return unresolved


def dump_unresolved(placement: Placement, reason: str):
    DEBUG_THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = DEBUG_THUMBS_DIR / f"{placement.key().replace(':', '-').replace(' ', '_')}.png"
    placement.raster.save(thumb_path)
    LOGGER.info(f"{placement.key()}: unresolved ({reason}), thumbnail saved to {thumb_path}")


def validate_trip(variant: str, trip: TripDraft):
    for group in trip.groups:
        if len(group.placements) != group.expected_count:
            raise ValueError(f"{variant}:{trip.folder_name}: group {group.index} ({group.title}) holds {len(group.placements)} images but its header says {group.expected_count}")
        names = [placement.matched_path for placement in group.placements]
        if len(names) != len(set(names)):
            raise ValueError(f"{variant}:{trip.folder_name}: group {group.index} ({group.title}) matched the same file twice")

    if variant == "moments":
        claimed = {}
        for group in trip.groups:
            for placement in group.placements:
                previous = claimed.get(placement.matched_path)
                if previous:
                    raise ValueError(f"moments:{trip.folder_name}: {placement.matched_path} claimed by both group {previous} and group {group.index}")
                claimed[placement.matched_path] = group.index


def write_reference(variant: str, source_name: str, trip: TripDraft, folder: Path) -> Path:
    grouped = [group for group in trip.groups if not group.excluded]
    excluded_paths = [placement.matched_path for group in trip.groups if group.excluded for placement in group.placements]
    placements = [placement for group in trip.groups for placement in group.placements]
    referenced = {placement.matched_path for placement in placements}
    unreferenced = sorted(name for name in (path.name for path in folder.iterdir() if path.suffix.lower() in IMAGE_EXTENSIONS) if name not in referenced)
    differences = [placement.match_difference for placement in placements if placement.match_difference is not None]

    payload = {
        "dataset_id": trip.folder_name,
        "variant": variant,
        "source": source_name,
        "groups": [
            {"index": index, "title": group.title, "asset_paths": [placement.matched_path for placement in group.placements]}
            for index, group in enumerate(grouped, start=1)
        ],
        "excluded_paths": excluded_paths,
        "match_report": {
            "placements": len(placements),
            "matched_by_pixels": sum(1 for placement in placements if not placement.via_override),
            "via_override": sum(1 for placement in placements if placement.via_override),
            "max_difference": round(max(differences, default=0.0), 2),
            "unreferenced_files": unreferenced,
        },
    }
    output_path = REFERENCES_DIR / f"{trip.folder_name}_{variant}.json"
    output_path.write_text(json.dumps(payload, indent=2) + "\n")
    LOGGER.info(f"{variant}:{trip.folder_name}: {len(grouped)} groups, {len(excluded_paths)} excluded, {len(unreferenced)} dataset files unreferenced, written to {output_path}")
    return output_path


def convert(pdf_name: str, variant: str, overrides: dict) -> int:
    pdf_path = EVAL_SETS_DIR / pdf_name
    trips = parse_pdf(pdf_path, variant)
    unresolved = 0
    for trip in trips.values():
        folder = EVAL_SETS_DIR / trip.folder_name
        if not folder.is_dir():
            raise FileNotFoundError(f"{variant}:{trip.folder_name}: no matching folder under {EVAL_SETS_DIR}")

        candidates = candidate_variants(folder)
        if variant == "moments":
            failed = assign_placements([placement for group in trip.groups for placement in group.placements], candidates, overrides, set())
        else:
            failed = []
            for group in trip.groups:
                failed.extend(assign_placements(group.placements, candidates, overrides, set()))

        trip_unresolved = len(failed)
        for placement, closest in failed:
            dump_unresolved(placement, closest)

        if trip_unresolved:
            unresolved += trip_unresolved
            LOGGER.info(f"{variant}:{trip.folder_name}: {trip_unresolved} placements unresolved, reference not written")
        else:
            validate_trip(variant, trip)
            write_reference(variant, pdf_name, trip, folder)

    total = sum(len(group.placements) for trip in trips.values() for group in trip.groups)
    LOGGER.info(f"{pdf_name}: {len(trips)} trips, {total} image placements, {unresolved} unresolved")
    return unresolved


def main():
    configure_logging("DEBUG")
    logging.getLogger("PIL").setLevel(logging.INFO)
    logging.getLogger("pdfminer").setLevel(logging.INFO)
    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    overrides = json.loads(OVERRIDES_PATH.read_text()) if OVERRIDES_PATH.is_file() else {}
    unresolved = sum(convert(pdf_name, variant, overrides) for pdf_name, variant in SOURCES)
    if unresolved:
        raise SystemExit(f"{unresolved} placements unresolved; inspect {DEBUG_THUMBS_DIR} and pin them in {OVERRIDES_PATH}")


if __name__ == "__main__":
    main()
