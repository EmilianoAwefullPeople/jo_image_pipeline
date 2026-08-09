import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jo_pipeline.extract import EXTRACTOR_VERSION, VISUAL_VERSION, RawExtraction

LOGGER = logging.getLogger(__name__)

NORMALIZER_VERSION = "normalize-1"
EXIF_TIMESTAMP_FORMAT = "%Y:%m:%d %H:%M:%S"
CAPTURE_TIME_CANDIDATES = ("DateTimeOriginal", "DateTimeDigitized", "DateTime")
SOUTHERN_REFERENCES = ("S", "W")
BELOW_SEA_LEVEL = "01"

PHOTO_CATEGORY = "photo"
DETECTED_CATEGORY = "detected"
COMPUTED_CATEGORY = "computed"

DEVICE_FIELDS = {
    "device_make": "Make",
    "device_model": "Model",
    "lens_model": "LensModel",
}
VISUAL_FIELDS = ("palette", "blur_score", "brightness", "difference_hash")


@dataclass(frozen=True)
class MetadataObservation:
    field: str
    category: str
    value: object
    raw_value: object
    source: str
    method_version: str
    confidence: float | None = None
    evidence: dict = field(default_factory=dict)
    unknown_reason: str | None = None


class MetadataNormalizer:
    def normalize(self, extraction: RawExtraction) -> list[MetadataObservation]:
        if extraction.failures:
            LOGGER.debug(f"{extraction.relative_path}: normalizing an extraction that recorded {len(extraction.failures)} failures")
            return [self._unknown("capture_local_time", PHOTO_CATEGORY, "exif", extraction.failures[0])]

        observations = self._photo_observations(extraction)
        observations.extend(self._computed_observations(observations))
        observations.extend(self._detected_observations(extraction))
        LOGGER.debug(f"{extraction.relative_path}: normalized {len(observations)} observations")
        return observations

    def _photo_observations(self, extraction: RawExtraction) -> list[MetadataObservation]:
        observations = [
            self._capture_local_time(extraction),
            self._utc_offset(extraction),
            self._coordinate(extraction, "gps_latitude", "GPSLatitude", "GPSLatitudeRef"),
            self._coordinate(extraction, "gps_longitude", "GPSLongitude", "GPSLongitudeRef"),
            self._altitude(extraction),
            self._orientation(extraction),
        ]
        observations.extend(self._device_observations(extraction))
        observations.extend(self._dimension_observations(extraction))
        return observations

    def _capture_local_time(self, extraction: RawExtraction) -> MetadataObservation:
        present = [name for name in CAPTURE_TIME_CANDIDATES if extraction.exif_tags.get(name)]
        if not present:
            return self._unknown("capture_local_time", PHOTO_CATEGORY, "exif", "no capture timestamp tag present")

        selected = present[0]
        raw_value = extraction.exif_tags[selected]
        evidence = {"candidates_present": present, "selected": selected}
        try:
            parsed = datetime.strptime(str(raw_value), EXIF_TIMESTAMP_FORMAT)
        except ValueError:
            return self._unknown("capture_local_time", PHOTO_CATEGORY, f"exif.{selected}", f"unparseable timestamp {raw_value}", raw_value, evidence)

        return MetadataObservation(
            field="capture_local_time",
            category=PHOTO_CATEGORY,
            value=parsed.isoformat(),
            raw_value=raw_value,
            source=f"exif.{selected}",
            method_version=NORMALIZER_VERSION,
            confidence=1.0 if selected == CAPTURE_TIME_CANDIDATES[0] else 0.6,
            evidence=evidence,
        )

    def _utc_offset(self, extraction: RawExtraction) -> MetadataObservation:
        raw_value = extraction.exif_tags.get("OffsetTimeOriginal") or extraction.exif_tags.get("OffsetTime")
        if not raw_value:
            return self._unknown("capture_utc_offset", PHOTO_CATEGORY, "exif", "no offset tag present, timezone must be derived from GPS")

        return MetadataObservation(
            field="capture_utc_offset",
            category=PHOTO_CATEGORY,
            value=str(raw_value).strip(),
            raw_value=raw_value,
            source="exif.OffsetTimeOriginal",
            method_version=NORMALIZER_VERSION,
            confidence=1.0,
        )

    def _coordinate(self, extraction: RawExtraction, field_name: str, value_tag: str, reference_tag: str) -> MetadataObservation:
        raw_value = extraction.gps_tags.get(value_tag)
        reference = extraction.gps_tags.get(reference_tag)
        if not raw_value or not reference:
            return self._unknown(field_name, PHOTO_CATEGORY, "exif.gps", "no gps coordinate present")

        degrees, minutes, seconds = (float(part) for part in raw_value)
        decimal = degrees + minutes / 60 + seconds / 3600
        if str(reference).upper() in SOUTHERN_REFERENCES:
            decimal = -decimal

        return MetadataObservation(
            field=field_name,
            category=PHOTO_CATEGORY,
            value=round(decimal, 7),
            raw_value=raw_value,
            source=f"exif.gps.{value_tag}",
            method_version=NORMALIZER_VERSION,
            confidence=1.0,
            evidence={"reference": reference},
        )

    def _altitude(self, extraction: RawExtraction) -> MetadataObservation:
        raw_value = extraction.gps_tags.get("GPSAltitude")
        if raw_value is None:
            return self._unknown("gps_altitude", PHOTO_CATEGORY, "exif.gps", "no gps altitude present")

        reference = extraction.gps_tags.get("GPSAltitudeRef")
        metres = float(raw_value)
        if str(reference) == BELOW_SEA_LEVEL:
            metres = -metres

        return MetadataObservation(
            field="gps_altitude",
            category=PHOTO_CATEGORY,
            value=round(metres, 3),
            raw_value=raw_value,
            source="exif.gps.GPSAltitude",
            method_version=NORMALIZER_VERSION,
            confidence=0.5,
            evidence={"reference": reference},
        )

    def _orientation(self, extraction: RawExtraction) -> MetadataObservation:
        raw_value = extraction.exif_tags.get("Orientation")
        if raw_value is None:
            return self._unknown("orientation", PHOTO_CATEGORY, "exif", "no orientation tag present")

        return MetadataObservation(
            field="orientation",
            category=PHOTO_CATEGORY,
            value=int(raw_value),
            raw_value=raw_value,
            source="exif.Orientation",
            method_version=NORMALIZER_VERSION,
            confidence=1.0,
        )

    def _device_observations(self, extraction: RawExtraction) -> list[MetadataObservation]:
        observations = []
        for field_name, tag in DEVICE_FIELDS.items():
            raw_value = extraction.exif_tags.get(tag)
            if raw_value:
                observations.append(MetadataObservation(
                    field=field_name,
                    category=PHOTO_CATEGORY,
                    value=str(raw_value).strip(),
                    raw_value=raw_value,
                    source=f"exif.{tag}",
                    method_version=NORMALIZER_VERSION,
                    confidence=1.0,
                ))
            else:
                observations.append(self._unknown(field_name, PHOTO_CATEGORY, "exif", f"no {tag} tag present"))
        return observations

    def _dimension_observations(self, extraction: RawExtraction) -> list[MetadataObservation]:
        observations = []
        for field_name in ("width", "height"):
            raw_value = extraction.image_properties.get(field_name)
            observations.append(MetadataObservation(
                field=f"image_{field_name}",
                category=PHOTO_CATEGORY,
                value=raw_value,
                raw_value=raw_value,
                source="decoded_pixels",
                method_version=EXTRACTOR_VERSION,
                confidence=1.0,
            ))
        return observations

    def _computed_observations(self, observations: list[MetadataObservation]) -> list[MetadataObservation]:
        values = {observation.field: observation.value for observation in observations}
        local_time = values.get("capture_local_time")
        offset = values.get("capture_utc_offset")
        if local_time and offset:
            stamped = datetime.strptime(f"{local_time}{offset}", "%Y-%m-%dT%H:%M:%S%z")
            return [MetadataObservation(
                field="capture_timestamp_utc",
                category=COMPUTED_CATEGORY,
                value=stamped.astimezone(timezone.utc).isoformat(),
                raw_value=None,
                source="capture_local_time+capture_utc_offset",
                method_version=NORMALIZER_VERSION,
                confidence=1.0,
                evidence={"local_time": local_time, "offset": offset},
            )]

        reason = "no capture timestamp" if not local_time else "no utc offset, timezone candidate required"
        return [self._unknown("capture_timestamp_utc", COMPUTED_CATEGORY, "capture_local_time+capture_utc_offset", reason)]

    def _detected_observations(self, extraction: RawExtraction) -> list[MetadataObservation]:
        observations = []
        for field_name in VISUAL_FIELDS:
            raw_value = extraction.visual_signals.get(field_name)
            observations.append(MetadataObservation(
                field=field_name,
                category=DETECTED_CATEGORY,
                value=raw_value,
                raw_value=None,
                source="opencv",
                method_version=VISUAL_VERSION,
                confidence=1.0,
                evidence={"sample_max_edge": extraction.visual_signals.get("sample_max_edge")},
            ))
        return observations

    def _unknown(self, field_name: str, category: str, source: str, reason: str, raw_value: object = None, evidence: dict | None = None) -> MetadataObservation:
        LOGGER.debug(f"{field_name}: recorded as unknown because {reason}")
        return MetadataObservation(
            field=field_name,
            category=category,
            value=None,
            raw_value=raw_value,
            source=source,
            method_version=NORMALIZER_VERSION,
            evidence=evidence or {},
            unknown_reason=reason,
        )
