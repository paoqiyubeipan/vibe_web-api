# Vibe Work Bridge (experimental)

A small **local-first** OpenAI-style adapter that operates Mistral Vibe Work through its visible browser UI. It exposes `vibe-work-fast` / `vibe-work-think` text aliases plus `vibe-work-image` / `vibe-work-image-think` image aliases, and can:

- send text turns in the current Work conversation or a fresh one;
- upload **reference images** and ordinary **documents** (PDF, TXT, DOCX) from the request itself;
- return **images Work generates**, as base64 or as a downloadable URL.

This is a prototype, not an official Mistral API or a Vibe Code/API client. The official [Work/Code selection guide](https://docs.mistral.ai/vibe/choose-chat-work-code) describes the Fast/Think toggle in Work, and [Work safety guidance](https://docs.mistral.ai/vibe/work/safety-and-approvals) explains its human-approval flow.

## Why this uses the UI

Mistral documents Work as the unified chat-and-agent surface and documents a Fast/Think toggle there. Work can call connectors and pause for clarification or approval. This bridge therefore drives the user-facing Work page rather than inventing or calling private web endpoints. It never extracts browser cookies, auto-approves actions, or executes external actions itself.

The UI is not a stable API. A Mistral UI change can break selectors or response extraction; if mode selection cannot be verified, the request fails instead of silently using the wrong mode. Review Mistral's applicable terms and do not expose this bridge as a public service.

## Install and run

Requires Python 3.11+ and a desktop session for the first login.

```bash
cd /path/to/vibe-work-bridge
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
playwright install chromium
vibe-work-bridge
```

On first API use, Chromium opens the profile at `~/.vibe-work-bridge/profile`. Sign in to Mistral in that browser yourself, select **Work**, and accept Vibe's terms if it asks. The profile is local and persistent; the bridge does not read or export session cookies. Leave the browser window available for Work's own clarification and approval prompts.

Default bind: `0.0.0.0:8765`, headed Chromium, 300-second response wait, and **no API key**. That means any device that can reach this port can use your signed-in Vibe session. Keep it on your LAN and do not add a router port-forward. To make it local-only, set `HOST=127.0.0.1`; to require a key, set `VIBE_WORK_API_KEY` (startup logs a warning if you listen beyond loopback without one).

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Listen on all interfaces. Use `127.0.0.1` for local-only access. |
| `PORT` | `8765` | HTTP port. |
| `VIBE_WORK_URL` | `https://chat.mistral.ai/` | Work web-app entry point. |
| `VIBE_WORK_PROFILE_DIR` | `~/.vibe-work-bridge/profile` | Persistent Playwright browser profile. |
| `VIBE_WORK_HEADLESS` | `0` | Set to `1` only after login is already available in the profile. |
| `VIBE_WORK_TIMEOUT` | `300` | Maximum seconds to wait for the visible response. |
| `VIBE_WORK_SUBMISSION_TIMEOUT` | `15` | Maximum seconds to wait for the new user message to appear in Work before waiting for a response. |
| `VIBE_WORK_APPROVAL_WAIT` | `300` | Maximum seconds to wait while a Work approval prompt is visible. The bridge never clicks approval buttons. |
| `VIBE_WORK_UPLOAD_TIMEOUT` | `120` | Maximum seconds to confirm a reference image appeared in the composer. |
| `VIBE_WORK_RESET_INPUT` | `1` | Recheck that the composer is clean before uploads. It never clears drafts; all requests refuse to overwrite unrecognized composer text or attachments, even when set to `0`. |
| `VIBE_WORK_IMAGE_SETTLE_TIMEOUT` | `45` | Extra seconds to wait for an image after the reply text settles. |
| `VIBE_WORK_OUTPUT_DIR` | `~/.vibe-work-bridge/outputs` | Where generated images are saved. |
| `VIBE_WORK_ASSISTANT_SELECTOR` | `[data-message-author-role="assistant"], [data-testid*="assistant-message"]` | CSS selectors used to read the latest assistant response. |
| `VIBE_WORK_CHROMIUM` | auto-detected | Path to a system Chrome/Chromium binary. |
| `VIBE_WORK_ENTRY_TIMEOUT` | `25` | Seconds to wait for the edge bot-check to clear before reporting a block. |
| `VIBE_WORK_API_KEY` | unset | Optional Bearer token; when set, callers must send `Authorization: Bearer <key>`. Empty means no auth. |

### Why a system browser matters

Playwright's bundled Chromium is fingerprinted by Mistral's edge protection: the page loops forever on "Performing security verification", even from an IP where a normal browser loads fine. A system Chrome/Chromium build passes the same check from the same host, so the bridge auto-detects and prefers one (`/usr/bin/chromium`, `/usr/bin/chromium-browser`, `/usr/bin/google-chrome`, `/usr/bin/google-chrome-stable`, `/snap/bin/chromium`) and only falls back to Playwright's build when none exists. If `/usr/bin/chromium` is itself a symlink to Playwright's build, point `VIBE_WORK_CHROMIUM` at the real Google Chrome binary instead. If a challenge loop persists, open the window once and complete the check.

If you set `VIBE_WORK_API_KEY`, use a long random value (for example, `python -c 'import secrets; print(secrets.token_urlsafe(32))'`) and send `Authorization: Bearer <key>` with every API request. On another device, use the server computer's LAN IP, for example `http://192.168.1.25:8765/v1`, not `127.0.0.1`. A key is not a substitute for firewall rules.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Process readiness, bind host, whether a key is required. |
| `GET` | `/v1/models` | Text and image mode aliases. |
| `GET` | `/v1/session` | Browser/session self-check; sends nothing to Work. |
| `POST` | `/v1/chat/completions` | Text or multimodal (text + reference image + document) turn. |
| `POST` | `/v1/images/generations` | Image generation, with optional reference images. |
| `GET` | `/v1/images/{id}/content` | Download one saved image. |
| `POST` | `/v1/conversations` | Open a new Work conversation. |
| `GET` | `/v1/conversations/current` | Identify the conversation currently open. |

### Text

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"vibe-work-fast",
       "messages":[{"role":"user","content":"Summarize this."}]}'
```

Choose quick mode with `"model": "vibe-work-fast"`; alternatively pass `"mode": "fast"` or `"mode": "think"` (also accepts `"thinking"`). An explicit `mode` overrides the model suffix. Streaming is not supported.

After submitting, the bridge first confirms that the new user message is visible in the Work conversation, then waits for the assistant response. If that submission cannot be confirmed, it returns `submission_unconfirmed` and does not resubmit; check the Work conversation before sending the request again.

If `model` and `mode` are both omitted, chat defaults to `vibe-work-think` (Think).

### New conversation

Add `"new_conversation": true` to a chat request, or call the endpoint directly:

```bash
curl -X POST http://127.0.0.1:8765/v1/conversations \
  -H 'Content-Type: application/json' -d '{"reuse_empty": false}'
```

It clicks Work's **New Chat** control and requires a changed, non-empty conversation ID, an empty message area, and a clean composer before reporting success. If Work stays at a route such as `/work` without a verifiable ID, the bridge fails closed and will not send. `reuse_empty: true` can reuse an already-empty chat only when it has a valid ID; chat requests with `new_conversation: true` always require a distinct chat. Responses include `id`, `url`, `empty`, `message_count`, and `created_new` / `reused`.

### Reference images

Send OpenAI-style content parts. Images must be base64 `data:` URLs; remote URLs are rejected so a LAN-reachable bridge cannot be used to fetch arbitrary addresses:

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"vibe-work-think","messages":[{"role":"user","content":[
        {"type":"text","text":"这张图里是什么？"},
        {"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0..."}}
      ]}]}'
```

Up to 4 images per request, 15 MB each and 30 MB in total. PNG, JPEG, WebP, GIF, AVIF and BMP are accepted; the declared MIME type is checked against the file's magic bytes, so a mislabelled or non-image payload is rejected before anything reaches Work. The bridge waits until the upload is visible in the composer and fails with `attachment_upload_failed` rather than sending a prompt that silently lost its image.

### Documents

Ordinary documents travel the same way: as OpenAI-style `file` content parts, or in a top-level `documents` list when the turn is not mixed.

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"vibe-work-fast","messages":[{"role":"user","content":[
        {"type":"text","text":"用三句话总结这份文档。"},
        {"type":"file","file":{"filename":"brief.pdf",
          "file_data":"data:application/pdf;base64,JVBERi0xLjQ..."}}
      ]}]}'
```

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"vibe-work-fast",
       "documents":[{"filename":"note.txt","file_data":"data:text/plain;base64,U3ludGhldGlj..."}],
       "messages":[{"role":"user","content":"这份笔记的要点是什么？"}]}'
```

- PDF, plain text (UTF-8 or UTF-16) and DOCX. Legacy `.doc` is refused with a hint to save as `.docx`.
- Up to 4 documents per request, 15 MB each and 30 MB in total.
- `file_data` must be a `data:` URL or bare base64; remote URLs are refused for the same reason as images.
- The declared type is checked against the bytes, so a PDF sent as `text/plain` is rejected with `invalid_document` instead of being guessed at.
- DOCX must really contain `word/document.xml`; a renamed zip is refused.
- Filenames become a safe basename with the extension of the detected type, so `../../x.cmd` arrives as `x.txt`.
- Images and documents can be combined in one request; they go through Work's single **Upload Files** chooser, and each name must appear in the composer before the prompt is submitted.
- A document-only turn gets the default instruction “Read the attached document and summarize its key points.”
- The response contains Work's reply text plus a `documents` list with `filename`, `mime_type`, `bytes` and locally extracted `characters` — never the document body.

If Work's chooser refuses a type, the call fails with `attachment_upload_failed` instead of sending a prompt that lost its file. `GET /v1/session` reports `file_input_accept` and `document_upload_hint` (`documents_advertised`, `images_only`, `any_type`, `no_file_input`), so you can see what the current build advertises before uploading anything.

This is version 0.2.4. A bridge process started earlier keeps serving 0.2.3 until it is restarted.

### Image generation

```bash
curl http://127.0.0.1:8765/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"水墨风格的白猫，满月背景","size":"1024x1024"}'
```

```json
{
  "created": 1762046400,
  "data": [{
    "id": "3f6c…",
    "mime_type": "image/png",
    "width": 1024,
    "height": 1024,
    "url": "/v1/images/3f6c…/content",
    "b64_json": "iVBORw0KGgo…",
    "revised_prompt": "水墨风格的白猫，满月背景"
  }]
}
```

- `model` is `vibe-work-image` (Fast) or `vibe-work-image-think` (Think).
- If `model` and `mode` are omitted, image generation defaults to `vibe-work-image-think` (Think).
- `response_format: "url"` omits `b64_json`; the returned `url` is relative to the bridge.
- `reference_images: ["data:image/png;base64,…"]` (or the single `image` field) uploads reference images before the prompt, so Work can edit or restyle them.
- Every generated image is also written to `VIBE_WORK_OUTPUT_DIR`, and its id is a content hash. `/v1/images/{id}/content` is intentionally unauthenticated so an `<img>` tag can load it; the 128-bit hash is the capability.
- If Work answers with text but never renders an image, the request fails with HTTP 502 `image_not_generated` and includes Work's reply in `detail.work_reply` instead of pretending an image exists.

Each call returns `attachments` (how many reference images were uploaded) and `conversation` (the Work conversation the turn landed in).

### Session self-check

`GET /v1/session` opens (or reuses) the browser and reports what the page actually shows, without sending anything to Work:

```bash
curl http://127.0.0.1:8765/v1/session
```

```json
{
  "url": "https://chat.mistral.ai/work",
  "title": "Vibe Work",
  "signed_in": true,
  "work_composer_found": true,
  "terms_consent_required": false,
  "cloudflare_challenge": false,
  "mode": "fast",
  "conversation": {"id": null, "url": "https://chat.mistral.ai/work", "empty": true, "message_count": 0},
  "file_input_found": true,
  "file_input_accept": ["image/png,image/jpeg,application/pdf,text/plain,.docx"],
  "document_upload_hint": "documents_advertised",
  "new_chat_control_found": true
}
```

The flags tell you whether the upload and new-chat flows can currently find their controls on your build of the UI, and whether the file input advertises documents or images only.

### Error codes

Failures are reported as `{"detail": {"message", "type", "code"}}`:

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 401 | `login_required` | The bridge browser is not signed in to Mistral. |
| 412 | `terms_consent_required` | Vibe is waiting for the account holder to accept its terms. The bridge never accepts legal terms itself. |
| 503 | `cloudflare_challenge` | The edge bot-check never cleared; open the browser window or set `VIBE_WORK_CHROMIUM`. |
| 409 | `mode_switch_unverified` | The requested Fast/Think state could not be confirmed, so no reply is returned. |
| 409 | `work_composer_not_found` | The Work composer is not visible (wrong tab or changed UI). |
| 409 | `attachment_upload_failed` | An image or document never appeared in the composer; Work may have refused the file type. |
| 409 | `new_conversation_failed` | Work did not open an empty conversation. |
| 409 | `submission_unconfirmed` | The new user message did not appear; the bridge did not resubmit. Check the Work conversation before sending again. |
| 503 | `browser_closed` | The browser closed mid-request, so submission could not be confirmed. The bridge did not retry; check the Work conversation before submitting again. |
| 409 | `manual_work_approval` | Work is waiting for a human approval that the bridge will not click. |
| 422 | `invalid_reference_image` | Image is not a valid, matching `data:image/...` payload. |
| 422 | `invalid_document` | Document is not a valid, matching PDF, text or DOCX payload within the size limits. |
| 502 | `image_not_generated` | Work replied without rendering an image; see `detail.work_reply`. |
| 504 | `work_response_timeout` | No stable response before `VIBE_WORK_TIMEOUT`. |
| 400 | `stream_not_supported` | `stream: true` was requested. |

## Prototype limits

- One browser, one queue: requests are serialized on a single worker thread, so a long Work task blocks the next request.
- Conversations are addressed implicitly. `new_conversation` and `/v1/conversations` open a chat, but history is not synchronized: the bridge sends system/developer instructions plus the newest user message only.
- Requests stop with `composer_draft_conflict` when the composer already contains text or attachments the bridge cannot verify as its own. The bridge does not clear or overwrite these drafts; `VIBE_WORK_RESET_INPUT=0` does not disable this safety check.
- Reference images come from the newest user turn only, and image generation returns one image per request (`n` is fixed at 1).
- Documents are limited to PDF, text and DOCX; the bridge does not convert, OCR or split them, and it never reads a document body back out of Work.
- Generated images are read from the newest assistant message. If Work ever renders them outside that message, extraction fails loudly (`image_not_generated`) rather than guessing by page position.
- Work's connector actions remain in the browser workflow. If Work asks for a sensitive-action approval, you must approve or decline it in Work; the bridge never does so.
- No streaming, tool-call protocol, token accounting, or API-side conversation management.
- DOM selectors are validated against one signed-in session only; a UI redesign can break them.

## Tests

```bash
pytest
```

Unit tests cover mode parsing, prompt mapping, content-part handling, image and document validation, upload verification, new-conversation verification, image filtering, request/model contracts, blocker-to-error-code mapping, the session endpoint, and browser-executable selection.

`tests/test_browser_integration.py` goes further: it runs a real headless Chromium against `tests/fixtures/fake_work.html`, a small look-alike of the Work page, and checks the parts that only a browser can prove — mode switching, image and document upload confirmation, document text read back from the reply, new-chat verification, and reading a generated image out of an assistant message. It needs no login.

`scripts/verify_document_roundtrip.py` runs the same check for all three formats with clearly synthetic files and writes a JSON report to `test-artifacts/`. A live Mistral session is still the only way to confirm the real Work DOM and which file types its chooser actually accepts.
