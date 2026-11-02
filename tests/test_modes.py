import pytest

from vibe_work_bridge.api import ChatMessage, build_work_prompt
from vibe_work_bridge.modes import ResponseMode, mode_from_model, resolve_mode


def test_model_aliases_select_fast_and_think():
    assert mode_from_model("vibe-work-fast") is ResponseMode.FAST
    assert mode_from_model("mistral-vibe-work-thinking") is ResponseMode.THINK


def test_image_aliases_map_to_fast_and_think():
    assert mode_from_model("vibe-work-image") is ResponseMode.FAST
    assert mode_from_model("vibe-work-image-think") is ResponseMode.THINK
    assert resolve_mode("vibe-work-image", "think") is ResponseMode.THINK


def test_explicit_mode_overrides_valid_model_alias():
    assert resolve_mode("vibe-work-think", "fast") is ResponseMode.FAST
    assert resolve_mode("vibe-work-fast", "thinking") is ResponseMode.THINK


def test_unknown_model_is_rejected_even_when_mode_is_explicit():
    with pytest.raises(ValueError, match="unsupported model"):
        resolve_mode("unrecognized-model", "think")


def test_build_prompt_keeps_system_and_developer_instructions_and_latest_user_turn():
    prompt = build_work_prompt(
        [
            ChatMessage(role="system", content="Answer in Chinese."),
            ChatMessage(role="user", content="Earlier turn."),
            ChatMessage(role="assistant", content="Earlier answer."),
            ChatMessage(role="developer", content="Be concise."),
            ChatMessage(role="user", content="Compare these two plans."),
        ]
    )
    assert "Answer in Chinese." in prompt
    assert "Be concise." in prompt
    assert "Compare these two plans." in prompt
    assert "Earlier turn." not in prompt
    assert "Earlier answer." not in prompt


def test_build_prompt_requires_a_final_user_message():
    with pytest.raises(ValueError, match="end with a non-empty user message"):
        build_work_prompt(
            [
                ChatMessage(role="user", content="Hello"),
                ChatMessage(role="assistant", content="Hi"),
            ]
        )
