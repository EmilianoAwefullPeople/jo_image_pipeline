import base64
import io

from PIL import Image

from llm_pipeline.derivative import DATA_URL_PREFIX, DERIVATIVE_MAX_EDGE, DerivativeBuilder

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
DATETIME_ORIGINAL_TAG = 0x9003
ORIENTATION_TAG = 0x0112


def build_exif(capture_time=None, orientation=None, gps=False) -> Image.Exif:
    exif = Image.Exif()
    if capture_time is not None:
        exif[EXIF_IFD] = {DATETIME_ORIGINAL_TAG: capture_time}
    if orientation is not None:
        exif[ORIENTATION_TAG] = orientation
    if gps:
        exif[GPS_IFD] = {1: "N", 2: (31.0, 13.0, 0.0)}
    return exif


def write_image(root, name="IMG_0001.jpg", size=(4000, 3000), exif=None):
    image = Image.new("RGB", size, (120, 40, 200))
    if exif is None:
        image.save(root / name, format="JPEG")
    else:
        image.save(root / name, format="JPEG", exif=exif)
    return root / name


def decode_payload(derivative) -> bytes:
    return base64.b64decode(derivative.data_url.removeprefix(DATA_URL_PREFIX))


def test_a_large_image_is_downscaled_to_the_maximum_edge(tmp_path):
    write_image(tmp_path, size=(4000, 3000))

    derivative = DerivativeBuilder(tmp_path).build("IMG_0001.jpg")

    assert (derivative.width, derivative.height) == (DERIVATIVE_MAX_EDGE, 768)


def test_a_small_image_is_not_upscaled(tmp_path):
    write_image(tmp_path, size=(640, 480))

    derivative = DerivativeBuilder(tmp_path).build("IMG_0001.jpg")

    assert (derivative.width, derivative.height) == (640, 480)


def test_the_derivative_carries_no_exif_so_gps_never_leaves_the_machine(tmp_path):
    write_image(tmp_path, exif=build_exif(capture_time="2024:05:04 10:20:30", gps=True))

    derivative = DerivativeBuilder(tmp_path).build("IMG_0001.jpg")

    sent_image = Image.open(io.BytesIO(decode_payload(derivative)))
    assert dict(sent_image.getexif()) == {}


def test_orientation_is_applied_before_the_exif_is_dropped(tmp_path):
    # Orientation 6 rotates 90 degrees, so a landscape source must produce a portrait derivative
    write_image(tmp_path, size=(400, 200), exif=build_exif(orientation=6))

    derivative = DerivativeBuilder(tmp_path).build("IMG_0001.jpg")

    assert (derivative.width, derivative.height) == (200, 400)


def test_the_data_url_is_a_base64_encoded_jpeg(tmp_path):
    write_image(tmp_path, size=(100, 100))

    derivative = DerivativeBuilder(tmp_path).build("IMG_0001.jpg")

    payload = decode_payload(derivative)
    assert derivative.data_url.startswith(DATA_URL_PREFIX)
    assert Image.open(io.BytesIO(payload)).format == "JPEG"
    assert derivative.byte_size == len(payload)


def test_capture_time_is_surfaced_as_context_and_missing_exif_yields_none(tmp_path):
    write_image(tmp_path, name="with_time.jpg", exif=build_exif(capture_time="2024:05:04 10:20:30"))
    write_image(tmp_path, name="without_time.jpg")

    builder = DerivativeBuilder(tmp_path)

    assert builder.build("with_time.jpg").capture_local_time == "2024:05:04 10:20:30"
    assert builder.build("without_time.jpg").capture_local_time is None
