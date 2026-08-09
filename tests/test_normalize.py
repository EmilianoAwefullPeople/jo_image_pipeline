from jo_pipeline.extract import RawExtraction
from jo_pipeline.normalize import MetadataNormalizer


def build_extraction(exif_tags=None, gps_tags=None, failures=None):
    return RawExtraction(
        relative_path="IMG_0001.HEIC",
        image_properties={"format": "HEIF", "mode": "RGB", "width": 4032, "height": 3024},
        exif_tags=exif_tags or {},
        gps_tags=gps_tags or {},
        visual_signals={"palette": [], "blur_score": 1.0, "brightness": 2.0, "difference_hash": "00", "sample_max_edge": 512},
        failures=failures or [],
    )


def observation_for(observations, field):
    return next(observation for observation in observations if observation.field == field)


def test_capture_time_prefers_date_time_original():
    observations = MetadataNormalizer().normalize(build_extraction({"DateTimeOriginal": "2024:07:23 15:10:19", "DateTime": "2024:07:24 09:00:00"}))

    capture = observation_for(observations, "capture_local_time")
    assert capture.value == "2024-07-23T15:10:19"
    assert capture.source == "exif.DateTimeOriginal"
    assert capture.evidence["selected"] == "DateTimeOriginal"
    assert capture.confidence == 1.0


def test_capture_time_falls_back_and_records_the_weaker_source():
    observations = MetadataNormalizer().normalize(build_extraction({"DateTime": "2024:07:24 09:00:00"}))

    capture = observation_for(observations, "capture_local_time")
    assert capture.value == "2024-07-24T09:00:00"
    assert capture.source == "exif.DateTime"
    assert capture.confidence == 0.6
    assert capture.evidence["candidates_present"] == ["DateTime"]


def test_missing_capture_time_is_recorded_as_unknown_with_a_reason():
    observations = MetadataNormalizer().normalize(build_extraction({}))

    capture = observation_for(observations, "capture_local_time")
    assert capture.value is None
    assert capture.unknown_reason == "no capture timestamp tag present"


def test_utc_timestamp_is_computed_from_local_time_and_offset():
    observations = MetadataNormalizer().normalize(build_extraction({"DateTimeOriginal": "2024:07:23 15:10:19", "OffsetTimeOriginal": "+03:00"}))

    computed = observation_for(observations, "capture_timestamp_utc")
    assert computed.value == "2024-07-23T12:10:19+00:00"
    assert computed.category == "computed"


def test_utc_timestamp_stays_unknown_when_the_offset_is_missing():
    observations = MetadataNormalizer().normalize(build_extraction({"DateTimeOriginal": "2024:07:23 15:10:19"}))

    computed = observation_for(observations, "capture_timestamp_utc")
    assert computed.value is None
    assert computed.unknown_reason == "no utc offset, timezone candidate required"


def test_coordinates_convert_to_decimal_degrees():
    gps_tags = {
        "GPSLatitude": [37.0, 38.0, 42.08],
        "GPSLatitudeRef": "N",
        "GPSLongitude": [21.0, 19.0, 10.24],
        "GPSLongitudeRef": "E",
    }
    observations = MetadataNormalizer().normalize(build_extraction({}, gps_tags))

    assert observation_for(observations, "gps_latitude").value == 37.6450222
    assert observation_for(observations, "gps_longitude").value == 21.3195111


def test_southern_and_western_references_are_negated():
    gps_tags = {
        "GPSLatitude": [33.0, 51.0, 30.0],
        "GPSLatitudeRef": "S",
        "GPSLongitude": [70.0, 40.0, 0.0],
        "GPSLongitudeRef": "W",
    }
    observations = MetadataNormalizer().normalize(build_extraction({}, gps_tags))

    assert observation_for(observations, "gps_latitude").value == -33.8583333
    assert observation_for(observations, "gps_longitude").value == -70.6666667


def test_missing_coordinates_are_never_invented():
    observations = MetadataNormalizer().normalize(build_extraction({"DateTimeOriginal": "2024:07:23 15:10:19"}))

    latitude = observation_for(observations, "gps_latitude")
    assert latitude.value is None
    assert latitude.unknown_reason == "no gps coordinate present"


def test_a_failed_extraction_normalizes_to_a_single_unknown():
    observations = MetadataNormalizer().normalize(build_extraction(failures=["OSError: truncated file"]))

    assert len(observations) == 1
    assert observations[0].unknown_reason == "OSError: truncated file"
