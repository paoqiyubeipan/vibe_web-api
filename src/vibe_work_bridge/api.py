from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .browser import (
    ApprovalWaitTimeout,
    AttachmentUploadFailed,
    AuthenticationRequired,
    BrowserUnavailable,
    CloudflareChallenge,
    ComposerDraftConflict,
    ModeSwitchFailed,
    NewConversationFailed,
    ResponseTimeout,
    SubmissionNotConfirmed,
    TermsConsentRequired,
    VibeWorkBrowser,
    WorkUiNotFound,
)
from .documents import (
    DocumentAttachment,
    DocumentInputError,
    decode_documents,
)
from .images import (
    MAX_IMAGES,
    ImageAttachment,
    ImageInputError,
    decode_reference_images,
    find_saved_image,
    save_generated_image,
)
from .modes import ResponseMode, resolve_mode

logger = logging.getLogger("vibe_work_bridge")

DEFAULT_IMAGE_INSTRUCTION = "Describe this image."
DEFAULT_DOCUMENT_INSTRUCTION = "Read the attached document and summarize its key points."

Attachment = Union[ImageAttachment, DocumentAttachment]


# -- request models ---------------------------------------------------------


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageUrlObject(BaseModel):
    url: str
    detail: Literal["auto", "low", "high"] | None = None


class ImageUrlPart(BaseModel):
    type: Literal["image_url"]
    image_url: Union[ImageUrlObject, str]

    @field_validator("image_url", mode="before")
    @classmethod
    def _accept_plain_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"url": value}
        return value


class FileObject(BaseModel):
    """OpenAI-style file reference; ``data`` is accepted as a convenience alias."""

    filename: str | None = None
    file_data: str | None = None
    data: str | None = None
    mime_type: str | None = None

    def payload(self) -> str:
        value = (self.file_data or self.data or "").strip()
        if not value:
            raise ValueError("file_data is required for a document attachment")
        return value


class FilePart(BaseModel):
    type: Literal["file"]
    file: Union[FileObject, str]


ContentPart = Annotated[
    Union[TextPart, ImageUrlPart, FilePart], Field(discriminator="type")
]


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: Union[str, list[ContentPart]]


class ChatCompletionRequest(BaseModel):
    model: str = "vibe-work-think"
    messages: list[ChatMessage] = Field(min_length=1)
    mode: Literal["fast", "think", "thinking"] | None = None
    stream: bool = False
    new_conversation: bool = False
    documents: list[FileObject] = Field(default_factory=list, max_length=4)


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "vibe-work-image-think"
    mode: Literal["fast", "think", "thinking"] | None = None
    size: str | None = None
    response_format: Literal["b64_json", "url"] = "b64_json"
    reference_images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)
    image: str | None = None
    new_conversation: bool = False


class NewConversationRequest(BaseModel):
    reuse_empty: bool = False


# -- prompt building --------------------------------------------------------


def split_message_content(content: Any) -> tuple[str, list[str], list[FileObject]]:
    """Return (text, image data URLs, documents) for a string or content parts."""
    if isinstance(content, str):
        return content, [], []
    if content is None:
        return "", [], []
    texts: list[str] = []
    images: list[str] = []
    documents: list[FileObject] = []
    for part in content:
        if isinstance(part, TextPart):
            texts.append(part.text)
        elif isinstance(part, ImageUrlPart):
            url = part.image_url if isinstance(part.image_url, str) else part.image_url.url
            images.append(url)
        elif isinstance(part, FilePart):
            if isinstance(part.file, str):
                documents.append(FileObject(file_data=part.file))
            else:
                documents.append(part.file)
        else:  # pragma: no cover - pydantic already validates the shape
            raise ValueError(f"unsupported content part: {part!r}")
    return "\n".join(text for text in texts if text.strip()), images, documents


def build_work_prompt(messages: list[ChatMessage], *, has_documents: bool = False) -> str:
    """Use Work's current browser conversation; send only the newest user turn."""
    user_messages = [
        message
        for message in messages
        if message.role == "user"
        and any(
            (
                split_message_content(message.content)[0].strip(),
                split_message_content(message.content)[1],
                split_message_content(message.content)[2],
            )
        )
    ]
    if not messages or messages[-1].role != "user":
        raise ValueError("messages must end with a non-empty user message")
    latest_text, latest_images, latest_documents = split_message_content(messages[-1].content)
    has_files = bool(latest_images or latest_documents or has_documents)
    if not latest_text.strip() and not has_files:
        raise ValueError("messages must end with a non-empty user message")
    if not user_messages and not has_files:
        raise ValueError("messages must end with a non-empty user message")

    instructions = []
    for message in messages:
        if message.role in {"system", "developer"}:
            text, _, _ = split_message_content(message.content)
            if text.strip():
                instructions.append(text.strip())

    if latest_text.strip():
        latest_user = latest_text.strip()
    elif latest_images:
        latest_user = DEFAULT_IMAGE_INSTRUCTION
    else:
        latest_user = DEFAULT_DOCUMENT_INSTRUCTION
    if instructions:
        return (
            "Follow these conversation instructions where applicable:\n"
            + "\n\n".join(instructions)
            + "\n\nUser request:\n"
            + latest_user
        )
    return latest_user


def collect_reference_images(messages: list[ChatMessage]) -> list[str]:
    """Reference images are only taken from the newest user turn."""
    if not messages:
        return []
    _, images, _ = split_message_content(messages[-1].content)
    return images


def collect_reference_documents(
    messages: list[ChatMessage],
    extra: list[FileObject] | None = None,
) -> list[DocumentAttachment]:
    """Documents come from the newest user turn plus the request-level list."""
    items: list[FileObject] = []
    if messages:
        _, _, items = split_message_content(messages[-1].content)
    items = list(items) + list(extra or [])
    if not items:
        return []
    for item in items:
        if not (item.file_data or item.data or "").strip():
            raise DocumentInputError("each document needs file_data (base64 or data: URL)")
    return decode_documents(
        [
            {
                "filename": item.filename,
                "mime_type": item.mime_type,
                "file_data": item.payload(),
            }
            for item in items
        ]
    )


def build_image_prompt(prompt: str, size: str | None = None) -> str:
    cleaned = prompt.strip()
    lines = [
        "Create an image for this request.",
        f"Request: {cleaned}",
    ]
    if size:
        lines.append(f"Preferred image size or aspect ratio: {size}.")
    return "\n\n".join(lines)


# -- browser worker ---------------------------------------------------------


def _browser_was_closed(exc: BaseException) -> bool:
    """True when Playwright lost its page/context, e.g. the window was closed."""
    text = str(exc).lower()
    return type(exc).__name__ in {"TargetClosedError", "BrowserClosedError"} or (
        "target page, context or browser has been closed" in text
        or "browser has been closed" in text
    )


class BrowserWorker:
    """Keep the synchronous Playwright API on one dedicated worker thread."""

    def __init__(self, browser_factory=VibeWorkBrowser):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vibe-work-ui")
        self._browser_factory = browser_factory
        self._browser: VibeWorkBrowser | None = None
        self._closed = False

    @property
    def browser_started(self) -> bool:
        return self._browser is not None

    def _ensure_sync_browser(self) -> VibeWorkBrowser:
        if self._closed:
            raise BrowserUnavailable("The browser worker has been closed.")
        if self._browser is None:
            self._browser = self._browser_factory()
        return self._browser

    def _call_with_recovery(self, action):
        """Run one UI action, relaunching the browser once if it was closed."""
        try:
            return action(self._ensure_sync_browser())
        except Exception as exc:
            if not _browser_was_closed(exc):
                raise
            broken, self._browser = self._browser, None
            try:
                broken.close()
            except Exception:
                pass
            return action(self._ensure_sync_browser())

    def _send_sync(
        self,
        prompt: str,
        mode: ResponseMode,
        attachments: list[Attachment] | None,
        new_conversation: bool,
        expect_images: bool,
    ) -> dict:
        browser = self._ensure_sync_browser()
        try:
            return browser.send_message(
                prompt,
                mode,
                attachments=attachments,
                new_conversation=new_conversation,
                expect_images=expect_images,
            )
        except Exception as exc:
            if _browser_was_closed(exc):
                broken, self._browser = self._browser, None
                if broken is not None:
                    try:
                        broken.close()
                    except Exception:
                        pass
            # A closed page can fail after a click was accepted. Never replay
            # the whole message action; the next request may start a fresh page.
            raise

    async def send_message(
        self,
        prompt: str,
        mode: ResponseMode,
        *,
        attachments: list[Attachment] | None = None,
        new_conversation: bool = False,
        expect_images: bool = False,
    ) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            partial(
                self._send_sync,
                prompt,
                mode,
                attachments,
                new_conversation,
                expect_images,
            ),
        )

    def _inspect_sync(self) -> dict:
        return self._call_with_recovery(lambda browser: browser.inspect_session())

    async def inspect_session(self) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._inspect_sync)

    def _start_conversation_sync(self, reuse_empty: bool) -> dict:
        return self._call_with_recovery(
            lambda browser: browser.start_new_conversation(reuse_empty=reuse_empty)
        )

    async def start_conversation(self, *, reuse_empty: bool = False) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, partial(self._start_conversation_sync, reuse_empty)
        )

    def _conversation_state_sync(self) -> dict:
        return self._call_with_recovery(lambda browser: browser.current_conversation())

    async def conversation_state(self) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._conversation_state_sync)

    def _close_sync(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self._closed = True

    async def close(self) -> None:
        if self._closed:
            return
        if self._browser is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._close_sync)
        else:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)


# -- shared helpers ---------------------------------------------------------


def _verify_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
    """The key is optional by design: an empty VIBE_WORK_API_KEY disables auth."""
    expected = os.environ.get("VIBE_WORK_API_KEY")
    if not expected:
        return
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:]
    if provided != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")


def _api_error(status_code: int, message: str, error_type: str, code: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"message": message, "type": error_type, "code": code}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


_ERROR_MAP: tuple[tuple[type[BaseException], int, str, str], ...] = (
    (AuthenticationRequired, 401, "authentication_error", "login_required"),
    (TermsConsentRequired, 412, "consent_required", "terms_consent_required"),
    (CloudflareChallenge, 503, "edge_blocked", "cloudflare_challenge"),
    (ModeSwitchFailed, 409, "ui_state_error", "mode_switch_unverified"),
    (WorkUiNotFound, 409, "ui_state_error", "work_composer_not_found"),
    (ComposerDraftConflict, 409, "ui_state_error", "composer_draft_conflict"),
    (AttachmentUploadFailed, 409, "ui_state_error", "attachment_upload_failed"),
    (NewConversationFailed, 409, "ui_state_error", "new_conversation_failed"),
    (SubmissionNotConfirmed, 409, "ui_state_error", "submission_unconfirmed"),
    (ApprovalWaitTimeout, 409, "approval_required", "manual_work_approval"),
    (ResponseTimeout, 504, "timeout_error", "work_response_timeout"),
    (BrowserUnavailable, 503, "browser_error", "browser_unavailable"),
)


def _map_bridge_error(exc: BaseException) -> HTTPException:
    if _browser_was_closed(exc):
        return _api_error(
            503,
            "The bridge browser window closed while the request was running, so "
            "submission could not be confirmed. The bridge did not retry. Check "
            "the Work conversation before submitting again.",
            "browser_unavailable",
            "browser_closed",
        )
    for error_type, status, kind, code in _ERROR_MAP:
        if isinstance(exc, error_type):
            return _api_error(status, str(exc), kind, code)
    return _api_error(500, f"Unexpected bridge failure: {exc}", "server_error", "internal_error")


def _image_payload(image: Any, include_base64: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": image.image_id,
        "mime_type": image.mime_type,
        "url": image.url_path,
        "width": None,
        "height": None,
    }
    if include_base64:
        payload["b64_json"] = image.b64_json
    return payload


def _host_and_auth() -> tuple[str, bool]:
    host = os.environ.get("HOST", "0.0.0.0")
    return host, bool(os.environ.get("VIBE_WORK_API_KEY"))


# -- application ------------------------------------------------------------


def create_app(worker: BrowserWorker | None = None) -> FastAPI:
    browser_worker = worker or BrowserWorker()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await browser_worker.close()

    app = FastAPI(
        title="Vibe Work Bridge",
        description=(
            "Experimental local bridge driven through the visible Vibe Work browser UI. "
            "Supports text, reference-image uploads, image generation and new conversations."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.worker = browser_worker

    @app.get("/healthz")
    async def health():
        host, auth_required = _host_and_auth()
        return {
            "status": "ok",
            "version": __version__,
            "browser_started": browser_worker.browser_started,
            "bind_host": host,
            "auth_required": auth_required,
        }

    @app.get("/v1/models", dependencies=[Depends(_verify_api_key)])
    async def list_models():
        return {
            "object": "list",
            "data": [
                {"id": "vibe-work-fast", "object": "model", "created": 0, "owned_by": "mistral"},
                {"id": "vibe-work-think", "object": "model", "created": 0, "owned_by": "mistral"},
                {"id": "vibe-work-image", "object": "model", "created": 0, "owned_by": "mistral"},
                {"id": "vibe-work-image-think", "object": "model", "created": 0, "owned_by": "mistral"},
            ],
        }

    @app.get("/v1/session", dependencies=[Depends(_verify_api_key)])
    async def session_state(request: Request):
        """Report browser/session readiness without sending a Work message."""
        try:
            return await request.app.state.worker.inspect_session()
        except Exception as exc:  # mapped to a structured API error
            raise _map_bridge_error(exc) from exc

    @app.post("/v1/conversations", dependencies=[Depends(_verify_api_key)])
    async def create_conversation(request: Request, body: NewConversationRequest | None = None):
        """Open a fresh Work conversation and return its identity."""
        reuse_empty = bool(body.reuse_empty) if body is not None else False
        try:
            state = await request.app.state.worker.start_conversation(reuse_empty=reuse_empty)
        except Exception as exc:
            raise _map_bridge_error(exc) from exc
        return {
            "id": state.get("id") or f"work-{uuid.uuid4().hex[:12]}",
            "object": "conversation",
            "created": int(time.time()),
            "created_new": bool(state.get("created")),
            "reused": bool(state.get("reused")),
            "empty": bool(state.get("empty", True)),
            "message_count": int(state.get("message_count", 0)),
            "url": state.get("url", ""),
        }

    @app.get("/v1/conversations/current", dependencies=[Depends(_verify_api_key)])
    async def current_conversation(request: Request):
        try:
            state = await request.app.state.worker.conversation_state()
        except Exception as exc:
            raise _map_bridge_error(exc) from exc
        return {
            "id": state.get("id") or f"work-{uuid.uuid4().hex[:12]}",
            "object": "conversation",
            "empty": bool(state.get("empty", False)),
            "message_count": int(state.get("message_count", 0)),
            "url": state.get("url", ""),
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(_verify_api_key)])
    async def chat_completions(body: ChatCompletionRequest, request: Request):
        if body.stream:
            raise _api_error(
                400,
                "Streaming is not supported by this browser-UI prototype.",
                "invalid_request_error",
                "stream_not_supported",
            )
        try:
            mode = resolve_mode(body.model, body.mode)
            prompt = build_work_prompt(body.messages, has_documents=bool(body.documents))
            images = decode_reference_images(collect_reference_images(body.messages))
            documents = collect_reference_documents(body.messages, body.documents)
        except ImageInputError as exc:
            raise _api_error(422, str(exc), "invalid_request_error", "invalid_reference_image") from exc
        except DocumentInputError as exc:
            raise _api_error(422, str(exc), "invalid_request_error", "invalid_document") from exc
        except ValueError as exc:
            raise _api_error(422, str(exc), "invalid_request_error", "invalid_mode_or_messages") from exc

        attachments: list[Attachment] = [*images, *documents]
        try:
            result = await request.app.state.worker.send_message(
                prompt,
                mode,
                attachments=attachments,
                new_conversation=body.new_conversation,
                expect_images=False,
            )
        except Exception as exc:
            raise _map_bridge_error(exc) from exc

        response_mode = result.get("mode", mode.value)
        if result.get("mode_applied") is not True:
            raise _api_error(
                409,
                "The requested mode was not confirmed in the Vibe Work UI; no response will be returned as if it had been applied.",
                "ui_state_error",
                "mode_switch_unverified",
            )
        text = result.get("text", "")
        images = [save_generated_image(item["data"], item["mime_type"]) for item in result.get("images", [])]
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model,
            "mode": response_mode,
            "mode_applied": True,
            "conversation": result.get("conversation"),
            "attachments": result.get("attachments", len(attachments)),
            "documents": [document.describe() for document in documents],
            "images": [_image_payload(image, include_base64=True) for image in images],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            # Work's web UI does not expose token accounting to this bridge.
            "usage": None,
        }

    @app.post("/v1/images/generations", dependencies=[Depends(_verify_api_key)])
    async def generate_image(body: ImageGenerationRequest, request: Request):
        reference_urls = list(body.reference_images)
        if body.image:
            reference_urls.insert(0, body.image)
        try:
            mode = resolve_mode(body.model, body.mode)
            attachments = decode_reference_images(reference_urls)
        except ImageInputError as exc:
            raise _api_error(422, str(exc), "invalid_request_error", "invalid_reference_image") from exc
        except ValueError as exc:
            raise _api_error(422, str(exc), "invalid_request_error", "invalid_mode_or_messages") from exc

        prompt = build_image_prompt(body.prompt, body.size)
        try:
            result = await request.app.state.worker.send_message(
                prompt,
                mode,
                attachments=attachments,
                new_conversation=body.new_conversation,
                expect_images=True,
            )
        except Exception as exc:
            raise _map_bridge_error(exc) from exc

        if result.get("mode_applied") is not True:
            raise _api_error(
                409,
                "The requested mode was not confirmed in the Vibe Work UI; no image will be returned.",
                "ui_state_error",
                "mode_switch_unverified",
            )
        extracted = result.get("images", [])
        if not extracted:
            raise _api_error(
                502,
                "Work replied without a generated image.",
                "image_generation_error",
                "image_not_generated",
                work_reply=result.get("text", ""),
            )

        include_base64 = body.response_format == "b64_json"
        data = []
        for item in extracted:
            image = save_generated_image(item["data"], item["mime_type"])
            entry = _image_payload(image, include_base64=include_base64)
            entry["width"] = item.get("width") or None
            entry["height"] = item.get("height") or None
            entry["revised_prompt"] = body.prompt
            data.append(entry)
        return {
            "created": int(time.time()),
            "conversation": result.get("conversation"),
            "attachments": result.get("attachments", len(attachments)),
            "data": data,
        }

    @app.get("/v1/images/{image_id}/content")
    async def image_content(image_id: str):
        """Serve a saved image. The id is a content hash, so it acts as its own token."""
        path = find_saved_image(image_id)
        if path is None or not Path(path).is_file():
            raise _api_error(404, "Unknown image id.", "invalid_request_error", "image_not_found")
        media_type = "image/png"
        suffix = path.suffix.lower().lstrip(".")
        media_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "webp": "image/webp",
            "gif": "image/gif",
            "avif": "image/avif",
            "bmp": "image/bmp",
        }.get(suffix, media_type)
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    return app


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    _, auth_required = _host_and_auth()
    if host not in {"127.0.0.1", "localhost", "::1"} and not auth_required:
        logger.warning(
            "Listening on %s without VIBE_WORK_API_KEY: any device that can reach this port "
            "can use the signed-in Vibe session. Keep it on a trusted LAN and do not port-forward it.",
            host,
        )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
