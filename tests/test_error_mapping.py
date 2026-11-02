import pytest
from fastapi.testclient import TestClient

from vibe_work_bridge.api import create_app
from vibe_work_bridge.browser import (
    AttachmentUploadFailed,
    AuthenticationRequired,
    CloudflareChallenge,
    ComposerDraftConflict,
    NewConversationFailed,
    SubmissionNotConfirmed,
    TermsConsentRequired,
    WorkUiNotFound,
    resolve_chromium_executable,
)


class StateWorker:
    """Worker stub that either reports a session state or raises a UI error."""

    def __init__(self, state=None, error=None):
        self.state = state or {}
        self.error = error
        self.calls = 0

    async def send_message(self, prompt, mode, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"text": "ok", "mode": mode.value, "mode_applied": True}

    async def inspect_session(self):
        if self.error is not None:
            raise self.error
        return self.state

    async def close(self):
        pass


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (CloudflareChallenge("challenge"), 503, "cloudflare_challenge"),
        (TermsConsentRequired("terms"), 412, "terms_consent_required"),
        (AuthenticationRequired("login"), 401, "login_required"),
        (WorkUiNotFound("no composer"), 409, "work_composer_not_found"),
        (ComposerDraftConflict("existing draft"), 409, "composer_draft_conflict"),
        (AttachmentUploadFailed("upload"), 409, "attachment_upload_failed"),
        (NewConversationFailed("no new chat"), 409, "new_conversation_failed"),
        (
            SubmissionNotConfirmed("not visible; was not retried"),
            409,
            "submission_unconfirmed",
        ),
    ],
)
def test_ui_blockers_map_to_distinct_error_codes(monkeypatch, error, status, code):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    with TestClient(create_app(worker=StateWorker(error=error))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "vibe-work-fast", "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_session_endpoint_reports_readiness_without_sending(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = StateWorker(
        state={
            "url": "https://chat.mistral.ai/chat",
            "title": "Vibe Chat",
            "signed_in": True,
            "work_composer_found": True,
            "terms_consent_required": False,
            "cloudflare_challenge": False,
            "mode": "fast",
        }
    )
    with TestClient(create_app(worker=worker)) as client:
        response = client.get("/v1/session")
    assert response.status_code == 200
    assert response.json()["signed_in"] is True
    assert worker.calls == 0


def test_session_endpoint_surfaces_cloudflare_block(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    with TestClient(create_app(worker=StateWorker(error=CloudflareChallenge("blocked")))) as client:
        response = client.get("/v1/session")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "cloudflare_challenge"


def test_explicit_chromium_path_wins(monkeypatch):
    monkeypatch.setenv("VIBE_WORK_CHROMIUM", "/opt/custom/chrome")
    assert resolve_chromium_executable("/explicit/chrome") == "/explicit/chrome"
    assert resolve_chromium_executable() == "/opt/custom/chrome"


def test_chromium_path_is_none_when_nothing_is_installed(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_CHROMIUM", raising=False)
    monkeypatch.setattr(
        "vibe_work_bridge.browser.Path.exists", lambda self: False
    )
    assert resolve_chromium_executable() is None
