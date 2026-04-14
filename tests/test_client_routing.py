"""Tests for client.py mesh routing and response extraction."""

import pytest

from localforge.client import _extract_content, task_type_context, _task_type_ctx


def test_extract_content_valid():
    data = {"choices": [{"message": {"content": "hello"}}]}
    assert _extract_content(data) == "hello"


def test_extract_content_error_response():
    data = {"error": {"message": "model not loaded"}}
    with pytest.raises(ValueError, match="Backend returned error"):
        _extract_content(data)


def test_extract_content_missing_choices():
    with pytest.raises(ValueError, match="Malformed"):
        _extract_content({"result": "oops"})


def test_extract_content_empty_choices():
    with pytest.raises(ValueError, match="Malformed"):
        _extract_content({"choices": []})


def test_extract_content_not_dict():
    with pytest.raises(ValueError, match="Unexpected response type"):
        _extract_content("just a string")


@pytest.mark.asyncio
async def test_task_type_context_sets_and_resets():
    assert _task_type_ctx.get() == "default"

    async with task_type_context("code"):
        assert _task_type_ctx.get() == "code"

    assert _task_type_ctx.get() == "default"


@pytest.mark.asyncio
async def test_task_type_context_resets_on_exception():
    with pytest.raises(RuntimeError):
        async with task_type_context("vision"):
            assert _task_type_ctx.get() == "vision"
            raise RuntimeError("boom")

    assert _task_type_ctx.get() == "default"


@pytest.mark.asyncio
async def test_chat_with_mock(mock_httpx_client):
    """Test that chat() works with the mock httpx client fixture."""
    from localforge.client import chat

    mock_httpx_client.set_chat_response("mocked answer")
    result = await chat("test prompt", use_cache=False)
    assert result == "mocked answer"


@pytest.mark.asyncio
async def test_chat_with_task_type(mock_httpx_client):
    """Test that task_type_context works end-to-end with chat()."""
    from localforge.client import chat

    mock_httpx_client.set_chat_response("code review result")
    async with task_type_context("code"):
        result = await chat("review this code", use_cache=False)
    assert result == "code review result"
