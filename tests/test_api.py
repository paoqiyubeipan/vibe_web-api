import base64
import io
import zipfile

from fastapi.testclient import TestClient

from vibe_work_bridge.api import create_app
from vibe_work_bridge.documents import DOCX_MIME, PDF_MIME, TEXT_MIME
from vibe_work_bridge.modes import ResponseMode

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

TXT = b"Synthetic bridge note.\nmarker: DOC-TXT-ALPHA\n"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def docx_bytes(marker: str = "DOC-DOCX-BETA") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>" + marker + "</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    return buffer.getvalue()


def data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


class FakeWorker:
    browser_started = False

    def __init__(self):
        self.calls = []

    async def send_message(self, prompt, mode, **kwargs):
        self.calls.append({"prompt": prompt, "mode": mode, **kwargs})
        return {
            "text": "mock Work reply",
            "mode": mode.value,
            "mode_applied": True,
            "attachments": len(kwargs.get("attachments") or []),
            "images": [],
            "conversation": {"id": "conv-1", "url": "https://chat.mistral.ai/work/conv-1", "empty": False},
        }

    async def close(self):
        pass


def test_chat_route_forwards_think_mode_and_returns_mode_metadata(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-think",
                "messages": [{"role": "user", "content": "Reason carefully."}],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "mock Work reply"
    assert payload["mode"] == "think"
    assert payload["mode_applied"] is True
    assert payload["conversation"]["id"] == "conv-1"
    assert worker.calls[0]["mode"] is ResponseMode.THINK
    assert worker.calls[0]["prompt"] == "Reason carefully."


def test_chat_defaults_to_think(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Use the default mode."}]},
        )
    assert response.status_code == 200
    assert response.json()["model"] == "vibe-work-think"
    assert worker.calls[0]["mode"] is ResponseMode.THINK


def test_explicit_mode_overrides_the_model_suffix(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "mode": "thinking",
                "messages": [{"role": "user", "content": "Deep task."}],
            },
        )
    assert response.status_code == 200
    assert worker.calls[0]["mode"] is ResponseMode.THINK


def test_unknown_model_and_streaming_are_rejected(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    with TestClient(create_app(worker=FakeWorker())) as client:
        bad_model = client.post(
            "/v1/chat/completions",
            json={"model": "not-a-model", "messages": [{"role": "user", "content": "Hi"}]},
        )
        stream = client.post(
            "/v1/chat/completions",
            json={"model": "vibe-work-fast", "stream": True, "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert bad_model.status_code == 422
    assert stream.status_code == 400


def test_models_route_lists_text_and_image_aliases(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    with TestClient(create_app(worker=FakeWorker())) as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["data"]} == {
        "vibe-work-fast",
        "vibe-work-think",
        "vibe-work-image",
        "vibe-work-image-think",
    }


def test_api_key_is_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("VIBE_WORK_API_KEY", "local-test-secret")
    with TestClient(create_app(worker=FakeWorker())) as client:
        missing = client.get("/v1/models")
        allowed = client.get(
            "/v1/models", headers={"Authorization": "Bearer local-test-secret"}
        )
    assert missing.status_code == 401
    assert allowed.status_code == 200


def test_api_key_may_be_left_empty_for_lan_binding(monkeypatch):
    monkeypatch.setenv("VIBE_WORK_API_KEY", "")
    monkeypatch.setenv("HOST", "0.0.0.0")
    with TestClient(create_app(worker=FakeWorker())) as client:
        models = client.get("/v1/models")
        health = client.get("/healthz")
    assert models.status_code == 200
    assert health.json()["auth_required"] is False
    assert health.json()["bind_host"] == "0.0.0.0"


def test_healthz_reports_when_a_key_is_required(monkeypatch):
    monkeypatch.setenv("VIBE_WORK_API_KEY", "local-test-secret")
    monkeypatch.setenv("HOST", "127.0.0.1")
    with TestClient(create_app(worker=FakeWorker())) as client:
        health = client.get("/healthz").json()
    assert health["auth_required"] is True
    assert health["bind_host"] == "127.0.0.1"


def test_api_refuses_to_claim_success_when_ui_mode_was_not_confirmed(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)

    class UnverifiedWorker(FakeWorker):
        async def send_message(self, prompt, mode, **kwargs):
            return {"text": "should not be returned as success", "mode": mode.value, "mode_applied": False}

    with TestClient(create_app(worker=UnverifiedWorker())) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "vibe-work-fast", "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "mode_switch_unverified"


def test_new_conversation_flag_reaches_the_worker(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "new_conversation": True,
                "messages": [{"role": "user", "content": "Fresh topic."}],
            },
        )
    assert response.status_code == 200
    assert worker.calls[0]["new_conversation"] is True


def test_chat_content_parts_upload_reference_image(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What is in this picture?"},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{PNG_BASE64}"},
                            },
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 200
    call = worker.calls[0]
    assert call["prompt"] == "What is in this picture?"
    assert len(call["attachments"]) == 1
    assert call["attachments"][0].mime_type == "image/png"
    assert response.json()["attachments"] == 1


def test_remote_image_url_is_rejected(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    with TestClient(create_app(worker=FakeWorker())) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Look"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "http://192.168.1.1/secret.png"},
                            },
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_reference_image"


def test_image_only_user_turn_gets_a_default_instruction(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": f"data:image/png;base64,{PNG_BASE64}",
                            }
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 200
    assert worker.calls[0]["prompt"] == "Describe this image."
    assert len(worker.calls[0]["attachments"]) == 1
    assert base64.b64decode(PNG_BASE64)[:8] == b"\x89PNG\r\n\x1a\n"


def test_file_part_uploads_a_document_and_reports_metadata(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Summarize this file."},
                            {
                                "type": "file",
                                "file": {
                                    "filename": "quarterly-report.txt",
                                    "file_data": data_url(TEXT_MIME, TXT),
                                },
                            },
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    call = worker.calls[0]
    assert call["prompt"] == "Summarize this file."
    assert len(call["attachments"]) == 1
    document = call["attachments"][0]
    assert document.mime_type == TEXT_MIME
    assert document.name == "quarterly-report.txt"
    assert payload["attachments"] == 1
    assert payload["documents"] == [
        {
            "filename": "quarterly-report.txt",
            "mime_type": TEXT_MIME,
            "bytes": len(TXT),
            "characters": len(TXT.decode()),
        }
    ]


def test_document_only_turn_gets_a_default_instruction(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "file",
                                "file": {
                                    "filename": "brief.docx",
                                    "file_data": data_url(DOCX_MIME, docx_bytes()),
                                },
                            }
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 200
    assert worker.calls[0]["prompt"] == (
        "Read the attached document and summarize its key points."
    )
    assert response.json()["documents"][0]["filename"] == "brief.docx"


def test_document_can_be_sent_in_the_documents_field(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "documents": [
                    {"filename": "paper.pdf", "file_data": data_url(PDF_MIME, PDF)}
                ],
                "messages": [{"role": "user", "content": "What does this paper argue?"}],
            },
        )
    assert response.status_code == 200
    uploaded = worker.calls[0]["attachments"]
    assert len(uploaded) == 1 and uploaded[0].mime_type == PDF_MIME
    assert response.json()["documents"][0]["mime_type"] == PDF_MIME


def test_invalid_document_returns_a_document_specific_error(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    with TestClient(create_app(worker=FakeWorker())) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "file",
                                "file": {
                                    "filename": "fake.pdf",
                                    "file_data": data_url(PDF_MIME, b"not a pdf at all"),
                                },
                            }
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_document"


def test_documents_with_an_empty_message_still_get_a_default_instruction(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "documents": [
                    {"filename": "note.txt", "file_data": data_url(TEXT_MIME, TXT)}
                ],
                "messages": [{"role": "user", "content": ""}],
            },
        )
    assert response.status_code == 200
    assert worker.calls[0]["prompt"] == (
        "Read the attached document and summarize its key points."
    )


def test_image_and_document_upload_together(monkeypatch):
    monkeypatch.delenv("VIBE_WORK_API_KEY", raising=False)
    worker = FakeWorker()
    with TestClient(create_app(worker=worker)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "vibe-work-fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Compare the picture with the note."},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{PNG_BASE64}"},
                            },
                            {
                                "type": "file",
                                "file": {
                                    "filename": "note.txt",
                                    "file_data": data_url(TEXT_MIME, TXT),
                                },
                            },
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 200
    attachments = worker.calls[0]["attachments"]
    assert [item.mime_type for item in attachments] == ["image/png", TEXT_MIME]
    assert response.json()["attachments"] == 2
