from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ehtg.api.app import create_app
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.settings import Settings

ORIGIN = "chrome-extension://" + "a" * 32
SOURCE = "https://e-hentai.org/g/123/abcdef0123/"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        owner_id="123456",
        channel_id="-1001234567890",
        database_path=tmp_path / "http.sqlite3",
        source_session_path=tmp_path / "source-session.json",
        allowed_origins=(ORIGIN,),
        allowed_hosts=("testserver",),
        max_body_bytes=1024,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        auth = client.app.state.auth
        result = client.post(
            "/api/v1/pairing/redeem",
            json={"code": auth.issue_code(), "device_name": "Test browser"},
            headers={"Origin": ORIGIN},
        )
        assert result.status_code == 200
        client.headers.update(
            {"Authorization": f"Bearer {result.json()['token']}", "Origin": ORIGIN}
        )
        yield client


def test_all_application_routes_require_auth(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        for route in (
            "capabilities",
            "status",
            "devices",
            "jobs",
            "drafts/00000000-0000-0000-0000-000000000000",
        ):
            response = client.get(f"/api/v1/{route}")
            assert response.status_code == 401
            assert response.json()["request_id"] == response.headers["x-request-id"]


def test_capabilities_report_actual_implementation(client: TestClient) -> None:
    result = client.get("/api/v1/capabilities").json()
    assert result["features"]["persistent_queue"] is True
    assert result["features"]["source_analysis"] is False
    assert result["features"]["telegram_publishing"] is False
    assert result["limits"]["max_body_bytes"] == 1024


def test_create_edit_publish_and_recover_response(client: TestClient, settings: Settings) -> None:
    created = client.post("/api/v1/drafts", json={"source_url": SOURCE})
    assert created.status_code == 202
    assert created.headers["etag"] == '"1"'
    draft = created.json()
    client.app.state.service.record_analysis(
        draft["id"], 1, GallerySnapshot("Fixture", ("1", "2", "3"))
    )
    ready = client.get(f"/api/v1/drafts/{draft['id']}")
    assert ready.headers["etag"] == '"2"'
    edited = client.patch(
        f"/api/v1/drafts/{draft['id']}",
        json={"page_selection": {"exclude": "2"}},
        headers={"If-Match": '"2"'},
    )
    assert edited.status_code == 200
    assert edited.json()["selected_pages"] == [1, 3]
    route = f"/api/v1/drafts/{draft['id']}/publish"
    headers = {"If-Match": edited.headers["etag"], "Idempotency-Key": "confirmation-1"}
    first = client.post(route, json={}, headers=headers)
    assert first.status_code == 202
    job_id = first.json()["job_id"]
    assert [
        page["source_page_index"] for page in client.get(f"/api/v1/jobs/{job_id}").json()["pages"]
    ] == [1, 3]
    with TestClient(create_app(settings)) as restarted:
        replay = restarted.post(route, json={}, headers={**client.headers, **headers})
        assert replay.status_code == 202
        assert replay.json() == first.json()


def test_unready_draft_is_never_accepted_for_publication(client: TestClient) -> None:
    draft = client.post("/api/v1/drafts", json={"source_url": SOURCE}).json()
    response = client.post(
        f"/api/v1/drafts/{draft['id']}/publish",
        json={},
        headers={"If-Match": '"1"', "Idempotency-Key": "early"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DRAFT_NOT_READY"


def test_rejected_body_does_not_echo_secrets(client: TestClient) -> None:
    secret = "do-not-echo-this-cookie-or-token"
    response = client.post("/api/v1/drafts", json={"source_url": SOURCE, "cookies": secret})
    assert response.status_code == 422
    assert secret not in response.text
    response = client.post(
        "/api/v1/pairing/redeem", json={"code": secret, "device_name": {"token": secret}}
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_body_size_limit_without_content_length(client: TestClient) -> None:
    response = client.post("/api/v1/drafts", content=iter([b"a" * 600, b"b" * 600]))
    assert response.status_code == 413
    assert response.json()["code"] == "RESOURCE_LIMIT"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_arbitrary_target_channel_is_rejected(client: TestClient) -> None:
    draft = client.post("/api/v1/drafts", json={"source_url": SOURCE}).json()
    response = client.post(
        f"/api/v1/drafts/{draft['id']}/publish",
        json={"channel_id": "other"},
        headers={"If-Match": '"1"', "Idempotency-Key": "bad-channel"},
    )
    assert response.status_code == 422
    assert client.get("/api/v1/jobs").json()["items"] == []


def test_token_is_bound_to_origin_and_can_be_revoked(client: TestClient) -> None:
    assert client.get("/api/v1/jobs", headers={"Origin": "https://e-hentai.org"}).status_code == 401
    device_id = client.get("/api/v1/devices").json()["items"][0]["id"]
    assert client.post(f"/api/v1/devices/{device_id}/revoke").status_code == 200
    assert client.get("/api/v1/jobs").status_code == 401


def test_cors_and_host_are_restricted(client: TestClient) -> None:
    allowed = client.options("/api/v1/jobs", headers={"Access-Control-Request-Method": "GET"})
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    denied = client.options(
        "/api/v1/jobs",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in denied.headers
    assert client.get("/healthz", headers={"Host": "evil.test"}).status_code == 400


def test_storage_failure_has_safe_error_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def fail(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("sensitive filesystem path")

    monkeypatch.setattr(client.app.state.service, "list_jobs", fail)
    response = client.get("/api/v1/jobs")
    assert response.status_code == 503
    assert "sensitive" not in response.text
    assert response.json()["retryable"] is True


def test_invalid_settings_fail_closed() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            owner_id="123456",
            channel_id="-100123",
            allowed_origins=("https://e-hentai.org",),
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, owner_id="123456", channel_id="-100123", allowed_hosts=("*",))


def test_analysis_cancel_retry_and_errors_are_visible(client: TestClient) -> None:
    draft = client.post("/api/v1/drafts", json={"source_url": SOURCE}).json()
    assert draft["analysis"]["state"] == "pending"
    route = f"/api/v1/drafts/{draft['id']}/analysis"
    cancelled = client.post(f"{route}/cancel", headers={"If-Match": '"1"'})
    assert cancelled.status_code == 200
    assert cancelled.json()["analysis"]["error_code"] == "ANALYSIS_CANCELLED"
    stale = client.post(f"{route}/retry", headers={"If-Match": '"1"'})
    assert stale.status_code == 409
    retried = client.post(f"{route}/retry", headers={"If-Match": cancelled.headers["etag"]})
    assert retried.status_code == 202
    assert retried.json()["state"] == "analyzing"
    assert retried.json()["analysis"]["state"] == "pending"


def test_source_status_contains_no_session_values(client: TestClient) -> None:
    status = client.get("/api/v1/status").json()
    assert status["source"]["enabled"] is False
    assert status["source"]["session"]["state"] == "missing_or_expired"
    assert status["worker"] == "analysis_only"
