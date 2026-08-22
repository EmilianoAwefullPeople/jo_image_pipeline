import hashlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import httpx
import pillow_heif
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from jo_pipeline.logging_setup import configure_logging
from jo_web.config import ACCEPTED_EXTENSIONS, WebConfig, load_web_config
from jo_web.registry import CREATED, SkippedFile, RunRegistry, UploadedFile
from jo_web.serialize import run_state_payload, run_summary_payload
from jo_web.service import PipelineService
from jo_web.worker import RunJanitor, RunWorker
from llm_pipeline.prompts import CAPTURE_TIME_PLACEHOLDER, custom_prompts, default_prompts, template_reject_reason

LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_FILENAME_LENGTH = 120
THUMBNAIL_KEY_LENGTH = 16
MAX_PROMPT_CHARS = 20000


class PromptOverride(BaseModel):
    system: str
    user_template: str


@dataclass(frozen=True)
class StoredUpload:
    size_bytes: int
    sha256: str


def safe_filename(raw: str | None) -> str | None:
    if not raw:
        return None
    name = Path(raw).name.strip()
    if not name or name in (".", "..") or name.startswith(".") or "\x00" in name:
        return None
    if len(name) > MAX_FILENAME_LENGTH:
        name = name[-MAX_FILENAME_LENGTH:]
    return name


def unique_filename(name: str, taken: set) -> str:
    if name not in taken:
        return name

    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while f"{stem} ({index}){suffix}" in taken:
        index += 1
    candidate = f"{stem} ({index}){suffix}"
    LOGGER.info(f"{name}: renamed to {candidate} to avoid overwriting an earlier upload")
    return candidate


def build_app(config: WebConfig | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = config or load_web_config()
    registry = RunRegistry(settings)
    service = PipelineService(settings, registry, transport=transport)
    worker = RunWorker(settings, registry, service)
    janitor = RunJanitor(settings, registry)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging(settings.log_level)
        cv2.setNumThreads(1)
        pillow_heif.register_heif_opener()
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        if settings.openrouter_api_key:
            LOGGER.info(f"visual evaluation enabled with model {settings.openrouter_model}")
        else:
            LOGGER.warning("JO_OPENROUTER_API_KEY is not set, runs will return metadata and grouping only")
        worker.start()
        janitor.start()
        yield
        janitor.stop()
        worker.stop()

    application = FastAPI(title="Journey Onward image pipeline", lifespan=lifespan)

    def require_run(run_id: str):
        state = registry.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="run not found or expired")
        return state

    @application.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    @application.get("/api/prompt")
    async def read_prompt() -> dict:
        prompts = default_prompts()
        return {
            "version": prompts.version,
            "system": prompts.system,
            "user_template": prompts.user_template,
            "placeholder": CAPTURE_TIME_PLACEHOLDER,
            "max_chars": MAX_PROMPT_CHARS,
        }

    @application.get("/api/runs")
    async def list_runs() -> dict:
        return {"runs": [run_summary_payload(state) for state in registry.snapshot()]}

    @application.post("/api/runs", status_code=201)
    async def create_run() -> dict:
        if registry.bytes_on_disk() > settings.disk_budget_bytes:
            LOGGER.warning(f"run rejected, {registry.bytes_on_disk()} bytes on disk exceeds the budget")
            raise HTTPException(status_code=503, detail="the demo is out of disk space, try again shortly")

        state = registry.create()
        return {
            "run_id": state.run_id,
            "created_utc": state.created_utc,
            "limits": {
                "max_files": settings.max_files,
                "max_file_bytes": settings.max_file_bytes,
                "max_run_bytes": settings.max_run_bytes,
                "accepted_extensions": sorted(ACCEPTED_EXTENSIONS),
            },
        }

    @application.post("/api/runs/{run_id}/files")
    async def upload_file(run_id: str, file: UploadFile) -> dict:
        state = require_run(run_id)
        if state.status != CREATED:
            raise HTTPException(status_code=409, detail="this run has already started")
        if len(state.files) >= settings.max_files:
            raise HTTPException(status_code=413, detail=f"this demo accepts at most {settings.max_files} files per run")

        name = safe_filename(file.filename)
        if name is None:
            raise HTTPException(status_code=400, detail="that filename cannot be used")
        if Path(name).suffix.lower() not in ACCEPTED_EXTENSIONS:
            registry.add_skipped(run_id, SkippedFile(filename=name, reason="not a supported image format"))
            return {"accepted": False, "filename": name, "reason": "not a supported image format"}

        name = unique_filename(name, registry.existing_names(run_id))
        target = settings.upload_dir(run_id) / name
        stored = await _stream_to_disk(file, target, state.uploaded_bytes, settings)
        reason = _reject_reason(target, settings)
        if reason:
            target.unlink(missing_ok=True)
            registry.add_skipped(run_id, SkippedFile(filename=name, reason=reason))
            return {"accepted": False, "filename": name, "reason": reason}

        key = stored.sha256[:THUMBNAIL_KEY_LENGTH]
        thumbnail = key if service.ensure_thumbnail(target, settings.thumbnail_dir(run_id) / f"{key}.jpg", name) else None
        uploaded = UploadedFile(filename=name, size_bytes=stored.size_bytes, thumbnail=thumbnail)
        registry.add_file(run_id, uploaded)
        return {"accepted": True, **asdict(uploaded)}

    @application.put("/api/runs/{run_id}/prompt")
    async def replace_prompt(run_id: str, override: PromptOverride) -> dict:
        state = require_run(run_id)
        if state.status != CREATED:
            raise HTTPException(status_code=409, detail="this run has already started")

        system = override.system.strip()
        user_template = override.user_template.strip()
        if not system or not user_template:
            raise HTTPException(status_code=400, detail="both the system prompt and the user template are required")
        if len(system) > MAX_PROMPT_CHARS or len(user_template) > MAX_PROMPT_CHARS:
            raise HTTPException(status_code=400, detail=f"a prompt is limited to {MAX_PROMPT_CHARS} characters")

        reason = template_reject_reason(user_template)
        if reason:
            LOGGER.warning(f"{run_id}: prompt rejected, {reason}")
            raise HTTPException(status_code=400, detail=reason)

        prompts = custom_prompts(system, user_template)
        registry.set_prompts(run_id, prompts)
        return {"run_id": run_id, "prompt_version": prompts.version}

    @application.post("/api/runs/{run_id}/start", status_code=202)
    async def start_run(run_id: str) -> dict:
        state = require_run(run_id)
        if not state.files:
            raise HTTPException(status_code=400, detail="upload at least one image before starting")
        if registry.claim_for_queue(run_id) is None:
            raise HTTPException(status_code=409, detail="this run has already started")

        worker.submit(run_id)
        return {"run_id": run_id, "status": "queued", "queue_depth": worker.depth()}

    @application.get("/api/runs/{run_id}")
    async def read_run(run_id: str) -> dict:
        return run_state_payload(require_run(run_id), worker.depth())

    @application.get("/api/runs/{run_id}/export")
    async def export_run(run_id: str) -> Response:
        payload = jsonable_encoder(run_state_payload(require_run(run_id), worker.depth()))
        headers = {"Content-Disposition": f'attachment; filename="jo-run-{run_id}.json"'}
        return Response(content=json.dumps(payload, indent=2), media_type="application/json", headers=headers)

    @application.get("/api/runs/{run_id}/thumbnails/{key}")
    async def read_thumbnail(run_id: str, key: str):
        require_run(run_id)
        if len(key) != THUMBNAIL_KEY_LENGTH or not all(character in "0123456789abcdef" for character in key):
            raise HTTPException(status_code=400, detail="not a valid thumbnail key")

        target = settings.thumbnail_dir(run_id) / f"{key}.jpg"
        if not target.is_file():
            raise HTTPException(status_code=404, detail="thumbnail not found")
        return FileResponse(target, media_type="image/jpeg")

    @application.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str) -> JSONResponse:
        require_run(run_id)
        registry.delete(run_id)
        return JSONResponse(status_code=200, content={"run_id": run_id, "deleted": True})

    application.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return application


async def _stream_to_disk(file: UploadFile, target: Path, already_uploaded: int, settings: WebConfig) -> StoredUpload:
    written = 0
    digest = hashlib.sha256()
    with target.open("wb") as handle:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > settings.max_file_bytes or already_uploaded + written > settings.max_run_bytes:
                handle.close()
                target.unlink(missing_ok=True)
                LOGGER.warning(f"{target.name}: rejected, upload exceeded the configured size limits")
                raise HTTPException(status_code=413, detail="that upload is larger than this demo allows")
            handle.write(chunk)
            digest.update(chunk)
    LOGGER.debug(f"{target.name}: wrote {written} bytes")
    return StoredUpload(size_bytes=written, sha256=digest.hexdigest())


def _reject_reason(target: Path, settings: WebConfig) -> str | None:
    try:
        with Image.open(target) as image:
            pixels = image.width * image.height
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as error:
        LOGGER.warning(f"{target.name}: rejected, could not be opened as an image, {type(error).__name__}")
        return "not a readable image"

    if pixels > settings.max_pixels:
        LOGGER.warning(f"{target.name}: rejected at {pixels} pixels, over the {settings.max_pixels} limit")
        return "image resolution is larger than this demo allows"

    LOGGER.debug(f"{target.name}: accepted at {pixels} pixels")
    return None
