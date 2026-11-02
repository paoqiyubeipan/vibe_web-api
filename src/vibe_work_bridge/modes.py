from __future__ import annotations

from enum import Enum


class ResponseMode(str, Enum):
    FAST = "fast"
    THINK = "think"

    @classmethod
    def parse(cls, value: str | ResponseMode) -> ResponseMode:
        if isinstance(value, cls):
            return value
        normalized = value.strip().lower()
        if normalized in {"think", "thinking"}:
            return cls.THINK
        if normalized == "fast":
            return cls.FAST
        raise ValueError("mode must be 'fast' or 'think' (also accepts 'thinking')")


def mode_from_model(model: str) -> ResponseMode:
    """Resolve the explicit mode suffix from a bridge model alias."""
    normalized = model.strip().lower()
    if normalized in {
        "vibe-work",
        "mistral-vibe-work",
        "vibe-work-fast",
        "mistral-vibe-work-fast",
        "vibe-work-image",
        "mistral-vibe-work-image",
    }:
        return ResponseMode.FAST
    if normalized in {
        "vibe-work-think",
        "vibe-work-thinking",
        "mistral-vibe-work-think",
        "mistral-vibe-work-thinking",
        "vibe-work-image-think",
        "vibe-work-image-thinking",
    }:
        return ResponseMode.THINK
    raise ValueError(
        "unsupported model; use vibe-work-fast, vibe-work-think, "
        "vibe-work-image or vibe-work-image-think"
    )


def resolve_mode(model: str, mode: str | None) -> ResponseMode:
    """Validate the model alias, then let an explicit mode take precedence."""
    default = mode_from_model(model)
    if mode is not None:
        return ResponseMode.parse(mode)
    return default
