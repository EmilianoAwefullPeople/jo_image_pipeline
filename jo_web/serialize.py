from dataclasses import asdict

from jo_web.registry import RunState


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
        },
        "failure_detail": state.failure_detail,
    }


def run_summary_payload(state: RunState) -> dict:
    return {
        "run_id": state.run_id,
        "created_utc": state.created_utc,
        "status": state.status,
        "files": len(state.files),
        "images_analysed": state.images_analysed,
        "groups": len(state.groups),
    }
