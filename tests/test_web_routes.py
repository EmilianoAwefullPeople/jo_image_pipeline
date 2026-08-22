import hashlib
import io

from fastapi.testclient import TestClient
from PIL import Image

from jo_web.app import build_app, safe_filename, unique_filename
from llm_pipeline.prompts import PROMPT_VERSION
from tests.test_web_service import build_config, build_transport


def build_client(tmp_path) -> TestClient:
    return TestClient(build_app(build_config(tmp_path), transport=build_transport([])))


def photo_bytes(colour: tuple = (30, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (120, 90), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_a_run_id_that_is_not_a_uuid_hex_is_refused_before_any_path_is_built(tmp_path):
    # An unvalidated id is a path traversal into the data directory
    config = build_config(tmp_path)
    with TestClient(build_app(config, transport=build_transport([]))) as client:
        real = client.post("/api/runs").json()["run_id"]
        for run_id in ("..", "%2e%2e", "not-a-run", "a" * 32, real.upper(), f"{real}x"):
            assert client.get(f"/api/runs/{run_id}").status_code >= 400
            assert client.delete(f"/api/runs/{run_id}").status_code >= 400

        assert config.run_dir(real).is_dir()
        assert config.runs_dir.is_dir()


def test_an_unsupported_format_is_reported_rather_than_silently_dropped(tmp_path):
    # Files that vanish without explanation read as lost data to a visitor
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]

        response = client.post(f"/api/runs/{run_id}/files", files={"file": ("notes.webp", photo_bytes(), "image/webp")})

        assert response.status_code == 200
        assert response.json()["accepted"] is False
        assert response.json()["reason"] == "not a supported image format"
        assert client.get(f"/api/runs/{run_id}").json()["skipped"][0]["filename"] == "notes.webp"


def test_same_named_uploads_are_kept_apart_instead_of_overwriting(tmp_path):
    # One file per request means a repeated name would otherwise silently replace the first
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]

        first = client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_2615.jpg", photo_bytes((10, 20, 30)), "image/jpeg")})
        second = client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_2615.jpg", photo_bytes((200, 180, 60)), "image/jpeg")})

        assert first.json()["filename"] == "IMG_2615.jpg"
        assert second.json()["filename"] == "IMG_2615 (2).jpg"
        assert len(client.get(f"/api/runs/{run_id}").json()["files"]) == 2


def test_an_accepted_upload_is_thumbnailed_so_the_set_can_be_shown_before_it_is_processed(tmp_path):
    # The page shows the set as a grid before Process is pressed, and browsers cannot render HEIC themselves
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]
        payload = photo_bytes()

        response = client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_0001.jpg", payload, "image/jpeg")})

        key = response.json()["thumbnail"]
        assert key == hashlib.sha256(payload).hexdigest()[:16]
        thumbnail = client.get(f"/api/runs/{run_id}/thumbnails/{key}")
        assert thumbnail.status_code == 200
        assert thumbnail.headers["content-type"] == "image/jpeg"
        assert client.get(f"/api/runs/{run_id}").json()["files"][0]["thumbnail"] == key


def test_uploads_keep_joining_the_set_until_it_is_started(tmp_path):
    # Process is a separate step, so a set is built up over several drops and closed only by start
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]
        client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_0001.jpg", photo_bytes((10, 20, 30)), "image/jpeg")})
        client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_0002.jpg", photo_bytes((200, 180, 60)), "image/jpeg")})
        assert len(client.get(f"/api/runs/{run_id}").json()["files"]) == 2

        assert client.post(f"/api/runs/{run_id}/start").status_code == 202

        late = client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_0003.jpg", photo_bytes(), "image/jpeg")})
        assert late.status_code == 409


def test_starting_a_run_twice_is_refused(tmp_path):
    # A double clicked button would otherwise collide on the immutable manifest
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]
        client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_0001.jpg", photo_bytes(), "image/jpeg")})

        assert client.post(f"/api/runs/{run_id}/start").status_code == 202
        assert client.post(f"/api/runs/{run_id}/start").status_code == 409


def test_starting_a_run_with_no_accepted_files_is_refused(tmp_path):
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]

        assert client.post(f"/api/runs/{run_id}/start").status_code == 400


def test_the_file_count_cap_is_enforced_at_upload(tmp_path):
    # The cap has to bite before processing starts, not after
    config = build_config(tmp_path)
    with TestClient(build_app(config)) as client:
        run_id = client.post("/api/runs").json()["run_id"]
        for index in range(config.max_files):
            client.post(f"/api/runs/{run_id}/files", files={"file": (f"IMG_{index:04d}.jpg", photo_bytes(), "image/jpeg")})

        response = client.post(f"/api/runs/{run_id}/files", files={"file": ("extra.jpg", photo_bytes(), "image/jpeg")})

        assert response.status_code == 413


def test_a_thumbnail_key_that_is_not_a_hash_prefix_is_refused(tmp_path):
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]

        assert client.get(f"/api/runs/{run_id}/thumbnails/..%2f..%2fetc").status_code >= 400
        assert client.get(f"/api/runs/{run_id}/thumbnails/not-a-hash").status_code == 400
        assert client.get(f"/api/runs/{run_id}/thumbnails/{'0' * 16}").status_code == 404


def test_dangerous_filenames_are_reduced_to_a_bare_name_or_rejected():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("/absolute/IMG_1.jpg") == "IMG_1.jpg"
    assert safe_filename("..") is None
    assert safe_filename(".hidden.jpg") is None
    assert safe_filename("") is None
    assert safe_filename(None) is None


def test_a_name_collision_takes_the_next_free_index():
    assert unique_filename("IMG.jpg", set()) == "IMG.jpg"
    assert unique_filename("IMG.jpg", {"IMG.jpg"}) == "IMG (2).jpg"
    assert unique_filename("IMG.jpg", {"IMG.jpg", "IMG (2).jpg"}) == "IMG (3).jpg"


def test_the_default_prompt_is_served_for_editing(tmp_path):
    # The editor has to start from what the server would actually send
    with build_client(tmp_path) as client:
        payload = client.get("/api/prompt").json()

        assert payload["version"] == PROMPT_VERSION
        assert "{capture_local_time}" in payload["user_template"]
        assert payload["placeholder"] == "{capture_local_time}"
        assert payload["system"].strip()


def test_an_edited_prompt_is_attached_to_the_run_that_will_use_it(tmp_path):
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]

        response = client.put(f"/api/runs/{run_id}/prompt", json={"system": "Say nothing.", "user_template": "At {capture_local_time}."})

        assert response.status_code == 200
        assert response.json()["prompt_version"] == "custom"
        assert client.get(f"/api/runs/{run_id}").json()["llm"]["prompt_version"] == "custom"


def test_an_unusable_or_empty_prompt_is_refused_before_the_run_starts(tmp_path):
    # A template that cannot be formatted would fail once per image instead
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]

        assert client.put(f"/api/runs/{run_id}/prompt", json={"system": "Say nothing.", "user_template": "At {when}."}).status_code == 400
        assert client.put(f"/api/runs/{run_id}/prompt", json={"system": "  ", "user_template": "At {capture_local_time}."}).status_code == 400
        assert client.put(f"/api/runs/{run_id}/prompt", json={"system": "x" * 20001, "user_template": "At {capture_local_time}."}).status_code == 400
        assert client.get(f"/api/runs/{run_id}").json()["llm"]["prompt_version"] == PROMPT_VERSION


def test_the_prompt_cannot_be_changed_once_the_run_has_started(tmp_path):
    # Swapping the prompt mid run would make the records lie about what was sent
    with build_client(tmp_path) as client:
        run_id = client.post("/api/runs").json()["run_id"]
        client.post(f"/api/runs/{run_id}/files", files={"file": ("IMG_0001.jpg", photo_bytes(), "image/jpeg")})
        client.post(f"/api/runs/{run_id}/start")

        response = client.put(f"/api/runs/{run_id}/prompt", json={"system": "Too late.", "user_template": "At {capture_local_time}."})

        assert response.status_code == 409


def test_health_reports_ok(tmp_path):
    with build_client(tmp_path) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
