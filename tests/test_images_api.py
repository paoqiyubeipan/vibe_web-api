import base64

import pytest
from fastapi.testclient import TestClient

from vibe_work_bridge.api import create_app
from vibe_work_bridge.browser import AttachmentUploadFailed, NewConversationFailed
from vibe_work_bridge.modes import ResponseMode

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
REFERENCE_URL = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"


class ImageWorker:
    def __init__(self, images=None, text="Here is your image.", error=None):
        self.calls = []
        self.images = images if images is not None else [
            {"key": "a", "mime_type": "image/png", "data": PNG, "width": 512, "height": 512}
        ]
        self.text = text
        self.error = error

    async def send_message(self, prompt, mode, **kwargs):
        self.calls.append({"prompt": prompt, "mode": mode, **kwargs})
        if self.error is not None:
            raise self.error
        return {
            "text": self.text,
            "mode": mode.value,
            "mode_applied": True,
            "attachments": len(kwargs.get("attachments") or []),
            "images": self.images,
            "conversation": {"id": "conv-9", "url": "https://chat.mistral.ai/work/conv-9"},
        }

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def temp_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_WORK_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)


def test_generation_returns_base64_and_a_download_url():
    worker = ImageWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/images/generations",
            json={"prompt": "a red fox in the snow", "size": "1024x1024"},
        )
        assert response.status_code == 200
        payload = response.json()
        entry = payload["data"][0]
        assert base64.b64decode(entry["b64_json"]) == PNG
        assert entry["mime_type"] == "image/png"
        assert entry["width"] == 512
        content = client.get(entry["url"])
        assert content.status_code == 200
        assert content.headers["content-type"] == "image/png"
        assert content.content == PNG

        call = worker.calls[0]
        assert call["mode"] is ResponseMode.THINK
        assert "a red fox in the snow" in call["prompt"]
        assert "1024x1024" in call["prompt"]
        assert call["expect_images"] is True


def test_url_response_format_omits_base64():
    with TestClient(create_app(worker=ImageWorker())) as client:
        response = client.post(
            "/v1/images/generations",
            json={"prompt": "a lighthouse", "response_format": "url"},
        )
    entry = response.json()["data"][0]
    assert "b64_json" not in entry
    assert entry["url"].startswith("/v1/images/")


def test_reference_images_are_validated_and_uploaded():
    worker = ImageWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/images/generations",
            json={
                "prompt": "paint this cat as a watercolour",
                "reference_images": [REFERENCE_URL],
            },
        )
    assert response.status_code == 200
    call = worker.calls[0]
    assert len(call["attachments"]) == 1
    assert call["attachments"][0].mime_type == "image/png"
    assert worker.calls[0]["new_conversation"] is False


def test_invalid_reference_image_returns_422_before_touching_the_browser():
    worker = ImageWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/images/generations",
            json={"prompt": "x", "reference_images": ["https://example.com/a.png"]},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_reference_image"
    assert worker.calls == []


def test_reply_without_an_image_is_reported_as_a_failure():
    worker = ImageWorker(images=[], text="I cannot create images.")
    with TestClient(create_app(worker=worker)) as client:
        response = client.post("/v1/images/generations", json={"prompt": "draw a cat"})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "image_not_generated"
    assert detail["work_reply"] == "I cannot create images."


def test_upload_failure_maps_to_a_distinct_error_code():
    worker = ImageWorker(error=AttachmentUploadFailed("upload blocked"))
    with TestClient(create_app(worker=worker)) as client:
        response = client.post("/v1/images/generations", json={"prompt": "draw a cat"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "attachment_upload_failed"


def test_unknown_image_id_returns_404():
    with TestClient(create_app(worker=ImageWorker())) as client:
        response = client.get("/v1/images/0123456789abcdef0123456789abcdef/content")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "image_not_found"


class ConversationWorker(ImageWorker):
    def __init__(self, error=None):
        super().__init__(error=error)
        self.started = []

    async def start_conversation(self, *, reuse_empty=False):
        self.started.append(reuse_empty)
        if self.error is not None:
            raise self.error
        return {
            "id": "conv-new",
            "url": "https://chat.mistral.ai/work/conv-new",
            "empty": True,
            "message_count": 0,
            "created": True,
            "reused": False,
        }

    async def conversation_state(self):
        return {
            "id": "conv-current",
            "url": "https://chat.mistral.ai/work/conv-current",
            "empty": False,
            "message_count": 4,
        }


def test_new_conversation_endpoint_reports_identity():
    worker = ConversationWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post("/v1/conversations", json={"reuse_empty": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "conv-new"
    assert payload["object"] == "conversation"
    assert payload["created_new"] is True
    assert payload["empty"] is True
    assert worker.started == [True]


def test_new_conversation_failure_is_explicit():
    worker = ConversationWorker(error=NewConversationFailed("no New Chat button"))
    with TestClient(create_app(worker=worker)) as client:
        response = client.post("/v1/conversations")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "new_conversation_failed"


def test_current_conversation_endpoint_returns_state():
    with TestClient(create_app(worker=ConversationWorker())) as client:
        response = client.get("/v1/conversations/current")
    assert response.status_code == 200
    assert response.json()["id"] == "conv-current"
    assert response.json()["message_count"] == 4
