import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

LOGGER = logging.getLogger(__name__)

DERIVATIVE_VERSION = "derivative-1"
DERIVATIVE_MAX_EDGE = 1024
DERIVATIVE_JPEG_QUALITY = 85
DATA_URL_PREFIX = "data:image/jpeg;base64,"
EXIF_IFD = 0x8769
DATETIME_ORIGINAL_TAG = 0x9003


@dataclass(frozen=True)
class Derivative:
    data_url: str
    width: int
    height: int
    byte_size: int
    capture_local_time: str | None


class DerivativeBuilder:
    def __init__(self, dataset_path: Path):
        pillow_heif.register_heif_opener()
        self.dataset_path = dataset_path

    def build(self, relative_path: str) -> Derivative:
        path = self.dataset_path / relative_path
        with Image.open(path) as image:
            capture_local_time = self._capture_local_time(image)
            upright = ImageOps.exif_transpose(image)
            sample = upright.convert("RGB")
            sample.thumbnail((DERIVATIVE_MAX_EDGE, DERIVATIVE_MAX_EDGE), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            sample.save(buffer, format="JPEG", quality=DERIVATIVE_JPEG_QUALITY)
            payload = buffer.getvalue()
            width = sample.width
            height = sample.height

        if capture_local_time is None:
            LOGGER.debug(f"{relative_path}: no capture time available for model context")
        else:
            LOGGER.debug(f"{relative_path}: capture time {capture_local_time} attached as model context")
        LOGGER.debug(f"{relative_path}: derivative {width}x{height} {len(payload)} bytes")
        return Derivative(
            data_url=DATA_URL_PREFIX + base64.b64encode(payload).decode(),
            width=width,
            height=height,
            byte_size=len(payload),
            capture_local_time=capture_local_time,
        )

    def _capture_local_time(self, image: Image.Image) -> str | None:
        value = image.getexif().get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL_TAG)
        if value is None:
            return None
        return str(value)
