import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy
import pillow_heif
from PIL import ExifTags, Image, UnidentifiedImageError
from PIL.TiffImagePlugin import IFDRational

from jo_pipeline.manifest import ManifestEntry

LOGGER = logging.getLogger(__name__)

EXTRACTOR_VERSION = "extract-1"
VISUAL_VERSION = "opencv-1"
SAMPLE_MAX_EDGE = 512
PALETTE_COLOURS = 5
PALETTE_ATTEMPTS = 3
PALETTE_SEED = 20260809
HASH_EDGE = 8
EXIF_IFD = 0x8769
GPS_IFD = 0x8825
IMAGE_MEDIA_PREFIX = "image/"


@dataclass(frozen=True)
class RawExtraction:
    relative_path: str
    image_properties: dict
    exif_tags: dict
    gps_tags: dict
    visual_signals: dict
    failures: list[str]


class PhotoExtractor:
    def __init__(self, source_root: Path):
        pillow_heif.register_heif_opener()
        self.source_root = source_root

    def extract(self, entry: ManifestEntry) -> RawExtraction:
        path = self.source_root / entry.relative_path
        if not entry.media_type.startswith(IMAGE_MEDIA_PREFIX):
            LOGGER.debug(f"{entry.relative_path}: skipped extraction for media type {entry.media_type}")
            return self._empty_extraction(entry, f"media type {entry.media_type} is not an image")

        try:
            with Image.open(path) as image:
                exif = image.getexif()
                image_properties = self._read_image_properties(image)
                exif_tags = self._read_exif_tags(exif)
                gps_tags = self._read_gps_tags(exif)
                visual_signals = self._read_visual_signals(image)
        except (OSError, UnidentifiedImageError, ValueError) as error:
            LOGGER.warning(f"{entry.relative_path}: extraction failed with {type(error).__name__} {error}")
            return self._empty_extraction(entry, f"{type(error).__name__}: {error}")

        LOGGER.debug(f"{entry.relative_path}: extracted {len(exif_tags)} exif tags and {len(gps_tags)} gps tags")
        return RawExtraction(
            relative_path=entry.relative_path,
            image_properties=image_properties,
            exif_tags=exif_tags,
            gps_tags=gps_tags,
            visual_signals=visual_signals,
            failures=[],
        )

    def _empty_extraction(self, entry: ManifestEntry, failure: str) -> RawExtraction:
        return RawExtraction(
            relative_path=entry.relative_path,
            image_properties={},
            exif_tags={},
            gps_tags={},
            visual_signals={},
            failures=[failure],
        )

    def _read_image_properties(self, image: Image.Image) -> dict:
        return {
            "format": image.format,
            "mode": image.mode,
            "width": image.width,
            "height": image.height,
        }

    def _read_exif_tags(self, exif: Image.Exif) -> dict:
        tags = {ExifTags.TAGS.get(code, str(code)): json_safe(value) for code, value in exif.items()}
        for code, value in exif.get_ifd(EXIF_IFD).items():
            tags[ExifTags.TAGS.get(code, str(code))] = json_safe(value)
        return tags

    def _read_gps_tags(self, exif: Image.Exif) -> dict:
        return {ExifTags.GPSTAGS.get(code, str(code)): json_safe(value) for code, value in exif.get_ifd(GPS_IFD).items()}

    def _read_visual_signals(self, image: Image.Image) -> dict:
        sample = sample_array(image)
        greyscale = cv2.cvtColor(sample, cv2.COLOR_RGB2GRAY)
        return {
            "palette": self._palette(sample),
            "blur_score": float(cv2.Laplacian(greyscale, cv2.CV_64F).var()),
            "brightness": float(greyscale.mean()),
            "difference_hash": difference_hash(greyscale),
            "sample_max_edge": SAMPLE_MAX_EDGE,
        }

    def _palette(self, sample: numpy.ndarray) -> list[dict]:
        cv2.setRNGSeed(PALETTE_SEED)
        pixels = sample.reshape(-1, 3).astype(numpy.float32)
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        unused_compactness, labels, centres = cv2.kmeans(pixels, PALETTE_COLOURS, None, criteria, PALETTE_ATTEMPTS, cv2.KMEANS_PP_CENTERS)
        counts = numpy.bincount(labels.flatten(), minlength=PALETTE_COLOURS)
        palette = []
        for index in numpy.argsort(counts)[::-1]:
            red, green, blue = (int(round(channel)) for channel in centres[index])
            palette.append({
                "hex": f"#{red:02x}{green:02x}{blue:02x}",
                "proportion": round(float(counts[index]) / float(len(pixels)), 4),
            })
        return palette


def sample_array(image: Image.Image) -> numpy.ndarray:
    sample = image.convert("RGB")
    sample.thumbnail((SAMPLE_MAX_EDGE, SAMPLE_MAX_EDGE), Image.Resampling.BILINEAR)
    return numpy.asarray(sample)


def difference_hash(greyscale: numpy.ndarray) -> str:
    resized = cv2.resize(greyscale, (HASH_EDGE + 1, HASH_EDGE), interpolation=cv2.INTER_AREA)
    bits = resized[:, 1:] > resized[:, :-1]
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def image_difference_hash(image: Image.Image) -> str:
    return difference_hash(cv2.cvtColor(sample_array(image), cv2.COLOR_RGB2GRAY))


def hamming_distance(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def json_safe(value: object) -> object:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, IFDRational):
        return rational_to_float(value)
    return str(value)


def rational_to_float(value: IFDRational) -> object:
    if value.denominator == 0:
        return str(value)
    return float(value)
