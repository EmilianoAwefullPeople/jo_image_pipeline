import logging
from dataclasses import asdict

from jo_pipeline.refine import build_llm_observations
from jo_pipeline.reliability import ReliabilityReport
from jo_web.registry import RunState
from llm_pipeline.prompts import PROMPT_VERSION
from llm_pipeline.store import run_name
from llm_pipeline.styles import style_for

LOGGER = logging.getLogger(__name__)

GPS_LABEL = "GPS Location"
GPS_FIELDS = ("gps_latitude", "gps_longitude")
FRIENDLY_LABELS = {
    "capture_local_time": "Capture Time (local)",
    "capture_utc_offset": "UTC Offset",
    "gps_altitude": "GPS Altitude",
    "orientation": "Orientation",
    "device_make": "Camera Make",
    "device_model": "Camera Model",
    "lens_model": "Lens Model",
    "image_width": "Image Width",
    "image_height": "Image Height",
    "timezone_candidate": "Timezone",
    "capture_timestamp_utc": "Capture Time (UTC)",
    "palette": "Colour Palette",
    "blur_score": "Blur Score",
    "brightness": "Brightness",
    "difference_hash": "Difference Hash",
    "general_description": "Description",
    "scene_setting_types": "Scene Setting",
    "landmark_candidate": "Landmark",
    "notable_subjects": "Notable Subjects",
    "focal_point_types": "Focal Points",
    "activity_types": "Activity",
    "environment_general_types": "Environment",
    "environment_specific_style": "Environment Style",
    "composition_types": "Composition",
    "weather_conditions": "Weather",
    "keyword_tags": "Keywords",
    "photographic_style_types": "Photographic Style",
    "why_tags": "Why Tags",
    "is_screenshot_or_document": "Screenshot or Document",
    "memory_keep_signal": "Keep Signal",
    "representative_quality": "Representative Quality",
}


def run_state_payload(state: RunState, queue_depth: int) -> dict:
    return {
        "run_id": state.run_id,
        "created_utc": state.created_utc,
        "status": state.status,
        "progress": {"stage": state.stage, "done": state.done, "total": state.total, "queue_depth": queue_depth},
        "files": [asdict(uploaded) for uploaded in state.files],
        "skipped": [asdict(skipped) for skipped in state.skipped],
        "extraction": {
            "images_analysed": state.images_analysed,
            "failures": state.reliability_failures,
            "rows": [dict(asdict(row), presence_rate=row.presence_rate) for row in state.reliability_rows],
        },
        "groups": [asdict(proposal) for proposal in state.groups],
        "baseline_groups": [asdict(proposal) for proposal in state.baseline_groups],
        "thumbnails": state.thumbnails,
        "llm": {
            "summary": None if state.llm_summary is None else asdict(state.llm_summary),
            "records": [asdict(record) for record in state.llm_records],
            "prompt_version": PROMPT_VERSION if state.prompts is None else state.prompts.version,
            "review": {
                "style": state.style,
                "summary": None if state.review_summary is None else asdict(state.review_summary),
                "records": state.review_records,
                "unassigned": state.review_unassigned,
            },
        },
        "failure_detail": state.failure_detail,
    }


def export_payload(state: RunState, queue_depth: int) -> dict:
    return dict(run_state_payload(state, queue_depth), aggregate=_aggregate(state), images=_image_details(state), styles=_style_payloads(state))


def _label(field_name: str) -> str:
    return FRIENDLY_LABELS.get(field_name, field_name.replace("_", " ").title())


def _gps_present(state: RunState) -> int:
    count = 0
    for asset in state.assets:
        if asset.failure:
            continue
        values = asset.values()
        if values.get("gps_latitude") is not None and values.get("gps_longitude") is not None:
            count += 1
    return count


def _aggregate(state: RunState) -> dict:
    coverage = {}
    for row in state.reliability_rows:
        if row.field in GPS_FIELDS:
            coverage.setdefault(GPS_LABEL, f"{_gps_present(state)}/{row.total}")
        else:
            coverage[_label(row.field)] = f"{row.present}/{row.total}"

    evaluated = [record for record in state.llm_records if record.evaluation is not None]
    if state.llm_summary is None or not evaluated:
        LOGGER.debug(f"{state.run_id}: no valid evaluations, coverage carries extraction fields only")
    else:
        method_version = run_name(state.llm_summary.prompt_version, state.llm_summary.schema_version)
        report = ReliabilityReport()
        for record in evaluated:
            report.add_asset(record.relative_path, build_llm_observations(record.evaluation, record.model, method_version))
        for row in report.rows():
            coverage[_label(row.field)] = f"{row.present}/{row.total}"
        LOGGER.debug(f"{state.run_id}: coverage includes llm fields over {len(evaluated)} valid evaluations")
    return {"images_analysed": state.images_analysed, "evaluations": len(evaluated), "coverage": coverage}


def _image_details(state: RunState) -> dict:
    evaluations = {record.sha256: record.evaluation for record in state.llm_records}
    images = {}
    for asset in state.assets:
        entry = asset.entry
        images[entry.relative_path] = {
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
            "media_type": entry.media_type,
            "failure": asset.failure,
            "observations": [asdict(observation) for observation in asset.observations],
            "evaluation": evaluations.get(entry.sha256),
        }
    return images


def _style_payloads(state: RunState) -> dict:
    styles = {}
    for result in state.style_results.values():
        styles[result.style] = {
            "style": result.style,
            "name": style_for(result.style).name,
            "groups": [asdict(proposal) for proposal in result.groups],
            "review": {
                "summary": asdict(result.summary),
                "records": result.records,
                "unassigned": result.unassigned,
            },
        }
    return styles


def run_summary_payload(state: RunState) -> dict:
    return {
        "run_id": state.run_id,
        "created_utc": state.created_utc,
        "status": state.status,
        "files": len(state.files),
        "images_analysed": state.images_analysed,
        "groups": len(state.groups),
    }
